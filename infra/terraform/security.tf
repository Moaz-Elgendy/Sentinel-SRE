# Security group for the K3s node.
#
# This is the file that enforces the exposure rules for this phase, so it
# is worth being explicit about what is deliberately absent:
#
#   NO port 22 (SSH).      Administration goes through SSM Session Manager,
#                          which is an *outbound* connection from the
#                          instance to the SSM service. It needs no inbound
#                          rule at all, which is precisely why it is used
#                          here — there is no SSH port to brute-force, and
#                          no key material to lose. There is also no
#                          `key_name` on the instance (see ec2.tf), so even
#                          if someone added an inbound 22 rule later there
#                          would be no authorized key to log in with.
#
#   NO port 6443 (K3s API). The Kubernetes API is reachable only from the
#                          instance itself (via the loopback kubeconfig)
#                          and through SSM. Nothing in this stack opens it,
#                          and CI reaches Kubernetes by asking SSM to run
#                          kubectl *on* the node rather than by connecting
#                          to the API from outside.
#
#   NO 5432 (PostgreSQL).  Both Postgres instances are ClusterIP Services
#                          inside K3s. They are not reachable from outside
#                          the cluster, let alone outside the VPC.
#
#   NO 9090/9093/3100/3000 (Prometheus / Alertmanager / Loki / Grafana).
#                          All ClusterIP. Reached over `kubectl
#                          port-forward` through SSM when a human needs
#                          them. See docs/aws-deployment.md.
#
# The only inbound rules are the public HTTP(S) entrypoint that Traefik
# serves the citizen portal on.
#
# Rules are declared as separate aws_vpc_security_group_*_rule resources
# rather than inline `ingress`/`egress` blocks. Inline blocks are
# authoritative and will silently delete any rule added out-of-band;
# separate resources let Terraform manage exactly the rules it created and
# report drift on them individually.

resource "aws_security_group" "k3s_node" {
  name        = "${var.project_name}-k3s-node"
  description = "Public HTTP(S) entrypoint for the Sentinel SRE demo K3s node. No SSH, no Kubernetes API."
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "${var.project_name}-k3s-node-sg"
  }

  lifecycle {
    # The SG is referenced by the instance's network interface; replacing it
    # in place would otherwise require detaching the instance first.
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "http" {
  for_each = toset(var.allowed_http_cidrs)

  security_group_id = aws_security_group.k3s_node.id
  description       = "Citizen portal via Traefik"
  cidr_ipv4         = each.value
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"

  tags = {
    Name = "${var.project_name}-ingress-http"
  }
}

resource "aws_vpc_security_group_ingress_rule" "https" {
  # Only created when TLS is actually configured — see the
  # enable_https_ingress variable for why an open-but-dead 443 is worse
  # than a closed one.
  for_each = var.enable_https_ingress ? toset(var.allowed_http_cidrs) : toset([])

  security_group_id = aws_security_group.k3s_node.id
  description       = "Citizen portal via Traefik (TLS)"
  cidr_ipv4         = each.value
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"

  tags = {
    Name = "${var.project_name}-ingress-https"
  }
}

# Unrestricted egress. The node genuinely needs broad outbound access:
# the K3s installer fetches from get.k3s.io, container images come from ECR
# and Docker Hub (postgres, prometheus, grafana, loki, alloy), apt runs
# during user_data, and the SSM agent maintains an outbound connection to
# the SSM service. Enumerating that as a CIDR allow-list would mean
# tracking AWS service prefix lists plus several third-party registries,
# which breaks the moment any of them changes IP range.
#
# The alternative that *would* let this be locked down is VPC endpoints for
# SSM and ECR, but those are ~$7/month each per AZ and there are four of
# them (ssm, ssmmessages, ec2messages, ecr.dkr + ecr.api + s3) — several
# times the cost of the instance. Explicitly out of scope for a
# cost-constrained demo; noted in docs/aws-deployment.md as the production
# hardening step.
resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.k3s_node.id
  description       = "Outbound to ECR, SSM, container registries, apt"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"

  tags = {
    Name = "${var.project_name}-egress-all"
  }
}
