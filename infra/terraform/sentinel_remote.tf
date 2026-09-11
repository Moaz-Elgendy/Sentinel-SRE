# A second, standalone EC2 instance running Sentinel OUTSIDE the K3s
# cluster — the actual external-control-plane topology, not the in-cluster
# Deployment in k8s/overlays/aws/sentinel/. Entirely gated behind
# var.enable_remote_sentinel (default false); every resource in this file
# is `count = var.enable_remote_sentinel ? 1 : 0`, so applying with the
# default leaves the existing single-instance environment completely
# unchanged.
#
# Same VPC, same public subnet as the K3s node (see network.tf's docstring
# for why there is no NAT Gateway / private subnet — same cost reasoning
# applies here). Sentinel gets a public IP for its OWN outbound access
# (SSM, ECR pull, OpenAI/Gemini/GitHub APIs) but is not reachable from the
# public internet on any port — see security.tf: the only inbound rule on
# aws_security_group.sentinel is the webhook port, sourced from the K3s
# node's security group specifically.
#
# ===========================================================================
# Credential handling — read this before applying
# ===========================================================================
# Sentinel authenticates to the remote K3s API with a Kubernetes
# ServiceAccount token (KUBERNETES_MODE=remote), scoped to exactly the Role
# in k8s/overlays/aws/sentinel/namespace-rbac.yaml — the same least-privilege
# Role whether Sentinel runs in-cluster or, as here, external to it.
#
# Terraform does NOT generate, store, or see the token value. The two SSM
# Parameters below are created EMPTY (a one-character placeholder) with
# `lifecycle { ignore_changes = [value] }`, specifically so that:
#   (a) the token is never written into Terraform state, and
#   (b) `terraform apply` never overwrites a value the operator put there
#       out-of-band.
# The operator puts the real value in with `aws ssm put-parameter
# --overwrite` AFTER generating it with `kubectl create token` on the K3s
# node — see docs/sentinel-remote-validation-runbook.md for the exact
# commands. This is a deliberate, explicitly temporary prototype credential
# per that runbook: a long-lived token, not backed by short-lived STS-style
# rotation, because Kubernetes ServiceAccount tokens do not have an AWS-
# style rotation mechanism built in. `kubectl create token` supports a
# --duration flag; the production evolution documented in the runbook is to
# re-issue and re-`put-parameter` on a schedule, or move to a proper
# workload-identity federation mechanism once one exists for this project.
#
# CA certificate: the K3s node's self-signed CA. Same treatment — an empty
# placeholder SSM Parameter, populated out-of-band. If left empty, Sentinel
# falls back to `verify_ssl=false` for the remote connection (see
# app/domain/environment.py's KubernetesConnectionConfig and
# app/clients/kubernetes_client.py's `_load_from_remote`) and LOGS A WARNING
# on every startup — acceptable for a prototype reached over a private VPC
# security-group-restricted connection, not for anything beyond that.

resource "aws_security_group" "sentinel" {
  count = var.enable_remote_sentinel ? 1 : 0

  name        = "${var.project_name}-sentinel"
  description = "External Sentinel control plane. No public inbound; webhook reachable only from the K3s node."
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "${var.project_name}-sentinel-sg"
  }

  lifecycle {
    create_before_destroy = true
  }
}

# Only the K3s node's Alertmanager needs to reach Sentinel, and only on the
# webhook/API port — sourced from the K3s node's security group, never a
# CIDR. No SSH, nothing else inbound at all: SSM administration needs no
# inbound rule (see security.tf's docstring for why).
resource "aws_vpc_security_group_ingress_rule" "sentinel_webhook_from_k3s" {
  count = var.enable_remote_sentinel ? 1 : 0

  security_group_id           = aws_security_group.sentinel[0].id
  description                 = "Alertmanager webhook + environment-registration API, from the K3s node only"
  referenced_security_group_id = aws_security_group.k3s_node.id
  from_port                   = var.sentinel_webhook_port
  to_port                     = var.sentinel_webhook_port
  ip_protocol                 = "tcp"

  tags = {
    Name = "${var.project_name}-sentinel-ingress-webhook"
  }
}

# Same broad-egress reasoning as the K3s node's aws_vpc_security_group_egress_rule.all
# (see security.tf): SSM, ECR pull, and outbound calls to whichever LLM
# provider/GitHub API is configured. Locking this down to specific
# endpoints would need the same VPC-endpoint spend called out there.
resource "aws_vpc_security_group_egress_rule" "sentinel_egress_all" {
  count = var.enable_remote_sentinel ? 1 : 0

  security_group_id = aws_security_group.sentinel[0].id
  description        = "Outbound to K3s node (API/Prometheus/Loki), SSM, ECR, LLM/GitHub APIs"
  cidr_ipv4          = "0.0.0.0/0"
  ip_protocol        = "-1"

  tags = {
    Name = "${var.project_name}-sentinel-egress-all"
  }
}

resource "aws_iam_role" "sentinel" {
  count = var.enable_remote_sentinel ? 1 : 0

  name        = "${var.project_name}-sentinel-role"
  description = "Instance role for the external Sentinel EC2: SSM administration, ECR pull, and read of exactly its own two credential parameters."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = {
    Name = "${var.project_name}-sentinel-role"
  }
}

resource "aws_iam_role_policy_attachment" "sentinel_ssm_core" {
  count = var.enable_remote_sentinel ? 1 : 0

  role       = aws_iam_role.sentinel[0].name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Same scoped ECR pull as the K3s node's role (iam.tf) — Sentinel's own
# image lives in the same registry as the four application services.
resource "aws_iam_role_policy" "sentinel_ecr_pull" {
  count = var.enable_remote_sentinel ? 1 : 0

  name   = "${var.project_name}-sentinel-ecr-pull"
  role   = aws_iam_role.sentinel[0].id
  policy = data.aws_iam_policy_document.ecr_pull.json
}

# Read (not write) access to exactly Sentinel's two credential parameters —
# nothing else in Parameter Store, nothing account-wide.
data "aws_iam_policy_document" "sentinel_ssm_read" {
  count = var.enable_remote_sentinel ? 1 : 0

  statement {
    sid    = "ReadOwnCredentialParameters"
    effect = "Allow"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
    ]
    resources = [
      aws_ssm_parameter.sentinel_k8s_token[0].arn,
      aws_ssm_parameter.sentinel_k8s_ca_cert[0].arn,
      aws_ssm_parameter.sentinel_extra_env[0].arn,
    ]
  }

  # SecureString parameters are encrypted with the account's default
  # aws/ssm KMS key; decrypting them needs kms:Decrypt on that key
  # specifically, not a broader KMS grant.
  statement {
    sid       = "DecryptDefaultSsmKey"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = ["arn:${data.aws_partition.current.partition}:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
  }
}

resource "aws_iam_role_policy" "sentinel_ssm_read" {
  count = var.enable_remote_sentinel ? 1 : 0

  name   = "${var.project_name}-sentinel-ssm-read"
  role   = aws_iam_role.sentinel[0].id
  policy = data.aws_iam_policy_document.sentinel_ssm_read[0].json
}

resource "aws_iam_instance_profile" "sentinel" {
  count = var.enable_remote_sentinel ? 1 : 0

  name = "${var.project_name}-sentinel-profile"
  role = aws_iam_role.sentinel[0].name

  tags = {
    Name = "${var.project_name}-sentinel-profile"
  }
}

# Empty placeholders — see the credential-handling docstring at the top of
# this file. "unset" is not a valid Kubernetes token or PEM cert, so
# Sentinel failing loudly (kubernetes_client_unavailable in the logs) is the
# correct behaviour until the operator populates these out-of-band.
resource "aws_ssm_parameter" "sentinel_k8s_token" {
  count = var.enable_remote_sentinel ? 1 : 0

  name        = "/${var.project_name}/sentinel/k8s-token"
  description = "Kubernetes ServiceAccount token for the external Sentinel. Populated out-of-band — see docs/sentinel-remote-validation-runbook.md. NOT managed by Terraform."
  type        = "SecureString"
  value       = "unset"

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Name = "${var.project_name}-sentinel-k8s-token"
  }
}

resource "aws_ssm_parameter" "sentinel_k8s_ca_cert" {
  count = var.enable_remote_sentinel ? 1 : 0

  name        = "/${var.project_name}/sentinel/k8s-ca-cert-b64"
  description = "Base64-encoded K3s cluster CA cert for the external Sentinel. Populated out-of-band; optional — empty means Sentinel connects with verify_ssl=false and logs a warning. NOT managed by Terraform."
  type        = "SecureString"
  value       = "unset"

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Name = "${var.project_name}-sentinel-k8s-ca-cert"
  }
}

# Free-form additional env vars — OPENAI_API_KEY / GEMINI_API_KEY /
# GITHUB_TOKEN / SLACK_WEBHOOK_URL / CHAOS_ADMIN_TOKEN, whichever the
# operator wants. One parameter rather than one-per-secret because all of
# them are optional and Sentinel already treats a missing one as "that
# integration is off" (see core/config.py) — no need to model each as its
# own Terraform resource. Raw KEY=VALUE lines, one per line, appended
# verbatim to the container's env file.
resource "aws_ssm_parameter" "sentinel_extra_env" {
  count = var.enable_remote_sentinel ? 1 : 0

  name        = "/${var.project_name}/sentinel/extra-env"
  description = "Optional KEY=VALUE lines (OPENAI_API_KEY, GITHUB_TOKEN, etc), one per line. Populated out-of-band. NOT managed by Terraform."
  type        = "SecureString"
  value       = "# add optional KEY=VALUE lines here, e.g.\n# OPENAI_API_KEY=sk-..."

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Name = "${var.project_name}-sentinel-extra-env"
  }
}

locals {
  sentinel_user_data = var.enable_remote_sentinel ? templatefile("${path.module}/sentinel_user_data.sh.tftpl", {
    aws_region       = var.aws_region
    ecr_registry     = local.ecr_registry
    ecr_repo_prefix  = var.project_name
    image_tag        = var.sentinel_image_tag
    k3s_private_ip   = aws_instance.k3s.private_ip
    webhook_port     = var.sentinel_webhook_port
    prometheus_port  = var.prometheus_nodeport
    loki_port        = var.loki_nodeport
    token_param_name = "/${var.project_name}/sentinel/k8s-token"
    ca_param_name    = "/${var.project_name}/sentinel/k8s-ca-cert-b64"
    extra_env_param_name = "/${var.project_name}/sentinel/extra-env"
  }) : ""
}

resource "aws_instance" "sentinel" {
  count = var.enable_remote_sentinel ? 1 : 0

  ami           = data.aws_ssm_parameter.ubuntu_2404.value
  instance_type = var.sentinel_instance_type
  subnet_id     = aws_subnet.public.id

  vpc_security_group_ids = [aws_security_group.sentinel[0].id]
  iam_instance_profile   = aws_iam_instance_profile.sentinel[0].name

  # No key_name — same reasoning as the K3s node: SSM Session Manager only,
  # no SSH port, no private key to lose.
  associate_public_ip_address = true

  root_block_device {
    volume_type           = "gp3"
    volume_size            = 12
    encrypted              = true
    delete_on_termination = true

    tags = {
      Name = "${var.project_name}-sentinel-root"
    }
  }

  metadata_options {
    http_endpoint                = "enabled"
    http_tokens                  = "required"
    http_put_response_hop_limit  = 1
    instance_metadata_tags       = "enabled"
  }

  user_data                   = local.sentinel_user_data
  user_data_replace_on_change = true

  tags = {
    Name = "${var.project_name}-sentinel"
    Role = "sentinel-control-plane"
  }

  depends_on = [
    aws_iam_role_policy.sentinel_ecr_pull,
    aws_iam_role_policy.sentinel_ssm_read,
    aws_iam_role_policy_attachment.sentinel_ssm_core,
    aws_instance.k3s,
    aws_vpc_security_group_ingress_rule.k8s_api_from_sentinel,
    aws_vpc_security_group_ingress_rule.prometheus_from_sentinel,
    aws_vpc_security_group_ingress_rule.loki_from_sentinel,
  ]
}
