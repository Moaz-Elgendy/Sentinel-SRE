# IAM for the EC2 instance.
#
# The instance gets an instance profile and no access keys. Nothing in this
# stack ever writes AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY anywhere —
# not into user_data, not into a Kubernetes Secret, not into the AMI. The
# instance obtains short-lived credentials from the EC2 Instance Metadata
# Service, which is exactly what an instance profile is for.
#
# Permissions are the minimum for the two things the node must do:
#   1. Be administered through SSM Session Manager (no SSH).
#   2. Pull container images from this project's ECR repositories.
#
# Note what is NOT granted: no AdministratorAccess, no ecr:* (push is a CI
# concern, not a node concern), no ec2:*, no iam:*, no s3:*, and no ability
# to describe or modify any other AWS resource.

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

resource "aws_iam_role" "k3s_node" {
  name        = "${var.project_name}-k3s-node-role"
  description = "Instance role for the Sentinel SRE demo K3s node: SSM administration and ECR pull only."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "ec2.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })

  tags = {
    Name = "${var.project_name}-k3s-node-role"
  }
}

# AWS-managed policy for SSM Session Manager. This is the one managed
# policy used here, because it is the documented minimum for a
# Session-Manager-managed instance and hand-rolling it means tracking the
# ssm/ssmmessages/ec2messages action sets as AWS changes them. It grants no
# permission to other AWS services and no permission to modify anything.
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.k3s_node.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# ECR pull, scoped to this project's repositories only.
#
# ecr:GetAuthorizationToken cannot be resource-scoped (it is an
# account-level call that returns a registry token, and IAM does not model
# a resource for it), so it is a separate statement with "*". The
# statements that actually read image data ARE scoped, so this role cannot
# pull from any repository outside this project.
data "aws_iam_policy_document" "ecr_pull" {
  statement {
    sid       = "GetRegistryAuthToken"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "PullProjectImages"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
    ]
    resources = [for repo in aws_ecr_repository.services : repo.arn]
  }
}

resource "aws_iam_role_policy" "ecr_pull" {
  name   = "${var.project_name}-ecr-pull"
  role   = aws_iam_role.k3s_node.id
  policy = data.aws_iam_policy_document.ecr_pull.json
}

resource "aws_iam_instance_profile" "k3s_node" {
  name = "${var.project_name}-k3s-node-profile"
  role = aws_iam_role.k3s_node.name

  tags = {
    Name = "${var.project_name}-k3s-node-profile"
  }
}
