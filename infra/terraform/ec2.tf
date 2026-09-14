# The single EC2 instance that is the whole cluster.
#
# One instance, control-plane and worker in one node. This is the cheapest
# arrangement that still gives a real Kubernetes API to develop and
# demonstrate against, which is the entire point of choosing K3s over EKS
# (EKS alone is ~$73/month for the control plane before a single node).

# Canonical's official Ubuntu 24.04 AMI id, published as a public SSM
# parameter. Read at plan time rather than hardcoded, so the environment
# gets a current, patched image instead of one pinned to whatever was
# newest the day this was written.
#
# This is a read of an AWS-published public parameter. It reads nothing
# belonging to this account and modifies nothing.
data "aws_ssm_parameter" "ubuntu_2404" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

locals {
  ecr_registry = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    aws_region      = var.aws_region
    aws_account_id  = data.aws_caller_identity.current.account_id
    ecr_registry    = local.ecr_registry
    ecr_repo_prefix = var.project_name
    k3s_version     = var.k3s_version
    app_namespace   = var.app_namespace
    repo_url        = var.repo_url == null ? "" : var.repo_url
    # Where the repository is checked out on the node. Passed in rather than
    # hardcoded in the template so the bootstrap script and the two helper
    # scripts it writes cannot disagree about the path.
    repo_dir = "/opt/sentinel-sre"
  })
}

resource "aws_instance" "k3s" {
  ami           = data.aws_ssm_parameter.ubuntu_2404.value
  instance_type = var.instance_type
  subnet_id     = aws_subnet.public.id

  vpc_security_group_ids = [aws_security_group.k3s_node.id]
  iam_instance_profile   = aws_iam_instance_profile.k3s_node.name

  # No key_name on purpose. There is no SSH ingress rule (see security.tf),
  # so a key pair would be dead weight — and not having one means there is
  # no private key anywhere that could be leaked or committed.
  # Administration is SSM Session Manager only.

  associate_public_ip_address = true

  root_block_device {
    volume_type = "gp3"
    volume_size = var.root_volume_size_gb

    # Encryption at rest with the AWS-managed EBS key. Free, and it means
    # the Postgres PersistentVolumes (which live on this disk via K3s's
    # local-path provisioner) are encrypted.
    encrypted = true

    # Keep the volume's lifecycle tied to the instance. This disk holds all
    # cluster state; an orphaned volume left behind after the instance is
    # gone is just a bill.
    delete_on_termination = true

    tags = {
      Name = "${var.project_name}-root"
    }
  }

  metadata_options {
    http_endpoint = "enabled"
    # IMDSv2 required. The instance profile's credentials are served by
    # IMDS, so making the token mandatory closes the SSRF-to-credential-
    # theft path that IMDSv1 leaves open — relevant here because the node
    # runs web-facing workloads.
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "enabled"
  }

  user_data                   = local.user_data
  user_data_replace_on_change = false

  # Guard against an accidental `terraform destroy` taking the cluster (and
  # every PersistentVolume on its root disk) with it. To intentionally tear
  # the environment down, set this to false and apply that change first.
  lifecycle {
    prevent_destroy = true

    ignore_changes = [
      # The AMI id from SSM moves whenever Canonical publishes a new build.
      # Without this, an unrelated `apply` weeks later would propose
      # replacing the instance — wiping the cluster — purely because a
      # patched AMI exists. Upgrading the OS should be a deliberate act:
      # remove this line, apply, and re-bootstrap.
      ami,
    ]
  }

  tags = {
    Name = "${var.project_name}-k3s-node"
    Role = "k3s-server"
  }

  depends_on = [
    # The bootstrap script's first ECR credential refresh needs the
    # repositories to exist, and needs the role policy that grants access
    # to be attached.
    aws_iam_role_policy.ecr_pull,
    aws_iam_role_policy_attachment.ssm_core,
    aws_ecr_repository.services,
    aws_route_table_association.public,
  ]
}
