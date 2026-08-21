# GitHub Actions -> AWS via OIDC federation.
#
# No long-lived AWS access keys exist anywhere in this design. GitHub
# Actions presents a short-lived OIDC token, AWS validates it against the
# provider below, and STS hands back credentials that expire with the job.
# There is nothing to rotate and nothing that stays valid if a workflow log
# leaks.
#
# Everything in this file is skipped when var.github_repository is null, so
# a first apply can bring the infrastructure up before CI is wired.

locals {
  create_github_role = var.github_repository != null

  github_oidc_url = "https://token.actions.githubusercontent.com"

  github_oidc_provider_arn = local.create_github_role ? (
    var.create_github_oidc_provider
    ? aws_iam_openid_connect_provider.github[0].arn
    : data.aws_iam_openid_connect_provider.github[0].arn
  ) : null
}

# The OIDC provider is an account-level singleton: only one can exist for
# token.actions.githubusercontent.com. If the account already has one
# (another project, or a previous apply), set create_github_oidc_provider =
# false and it is looked up instead — which also means this stack will not
# delete a provider other projects depend on.
resource "aws_iam_openid_connect_provider" "github" {
  count = local.create_github_role && var.create_github_oidc_provider ? 1 : 0

  url             = local.github_oidc_url
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]

  tags = {
    Name = "${var.project_name}-github-oidc"
  }
}

data "aws_iam_openid_connect_provider" "github" {
  count = local.create_github_role && !var.create_github_oidc_provider ? 1 : 0

  url = local.github_oidc_url
}

# Trust policy.
#
# The `sub` condition is the security boundary. Without it — or with a `*`
# in the wrong place — ANY GitHub Actions workflow in the world could
# assume this role. It is pinned to specific branches of one repository.
#
# `aud` is checked as well, because a `sub`-only condition can be satisfied
# by a token minted for a different audience.
data "aws_iam_policy_document" "github_assume_role" {
  count = local.create_github_role ? 1 : 0

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values = concat(
        [
          for branch in var.github_allowed_branches :
          "repo:${var.github_repository}:ref:refs/heads/${branch}"
        ],
        # A job bound to a GitHub Environment (e.g. `environment: aws-demo`)
        # presents repo:<owner>/<repo>:environment:<name> as its subject,
        # NOT the branch-ref form above — even when triggered from an
        # allowed branch. deploy-to-k3s and test-aws-connectivity both use
        # `environment: aws-demo`, so that subject must be trusted
        # explicitly or every OIDC assume-role call from those jobs fails.
        [
          "repo:${var.github_repository}:environment:${var.github_environment_name}"
        ]
      )
    }
  }
}

resource "aws_iam_role" "github_actions" {
  count = local.create_github_role ? 1 : 0

  name        = "${var.project_name}-github-actions"
  description = "Assumed by GitHub Actions in ${var.github_repository} to push images to ECR and trigger a deploy via SSM."

  assume_role_policy = data.aws_iam_policy_document.github_assume_role[0].json

  # 1 hour. Long enough for a build-and-push job, short enough that a
  # leaked credential is close to worthless.
  max_session_duration = 3600

  tags = {
    Name = "${var.project_name}-github-actions"
  }
}

# CI permissions: push images, and ask SSM to run the deploy script on the
# one instance. Notably absent — no ecr:DeleteRepository, no
# ecr:BatchDeleteImage (CI must not be able to destroy rollback targets),
# no ec2:*, no iam:*, and no ssm:SendCommand against arbitrary instances.
data "aws_iam_policy_document" "github_actions" {
  count = local.create_github_role ? 1 : 0

  statement {
    sid       = "GetRegistryAuthToken"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "PushProjectImages"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
      # Read actions too: buildx reads existing layers to skip re-uploading
      # them, and the workflow verifies the pushed manifest.
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
      "ecr:DescribeImages",
      "ecr:DescribeRepositories",
    ]
    resources = [for repo in aws_ecr_repository.services : repo.arn]
  }

  # Deploy trigger: run ONE specific SSM document against ONE specific
  # instance. AWS-RunShellScript is the document that executes the
  # /usr/local/bin/sentinel-deploy.sh helper baked into the node.
  #
  # This is the mechanism that keeps the Kubernetes API off the internet:
  # CI never connects to :6443. It asks SSM to run a command on the node,
  # and the node talks to its own local API server.
  statement {
    sid    = "TriggerDeployViaSSM"
    effect = "Allow"
    actions = [
      "ssm:SendCommand",
    ]
    resources = [
      aws_instance.k3s.arn,
      "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}::document/AWS-RunShellScript",
    ]
  }

  # Reading command results is a separate, resource-less API. Scoped by
  # action rather than resource because GetCommandInvocation does not
  # support resource-level permissions.
  statement {
    sid    = "ReadDeployResult"
    effect = "Allow"
    actions = [
      "ssm:GetCommandInvocation",
      "ssm:ListCommandInvocations",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "github_actions" {
  count = local.create_github_role ? 1 : 0

  name   = "${var.project_name}-github-actions"
  role   = aws_iam_role.github_actions[0].id
  policy = data.aws_iam_policy_document.github_actions[0].json
}
