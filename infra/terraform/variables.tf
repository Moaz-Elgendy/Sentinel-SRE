variable "aws_region" {
  description = <<-EOT
    The single AWS region this whole environment lives in. Everything —
    VPC, EC2, EBS, ECR — is created here; nothing is created in a second
    region. Change it before the first `apply`, not after (moving a running
    environment between regions means recreating it).

    Note: the default is a region where t3a instances are known to be
    available. Not every region offers t3a (the AMD-based burstable
    family); the Gulf regions (me-central-1, me-south-1) in particular may
    require switching `instance_type` to t3.large or m6i.large. If
    `apply` fails with an unsupported-instance-type error, that is why.
  EOT
  type        = string
  default     = "eu-central-1"
}

variable "environment" {
  description = "Environment name, used in the Environment tag and in resource names."
  type        = string
  default     = "demo"
}

variable "project_name" {
  description = <<-EOT
    Name prefix for every resource. Kept short because it is also the
    prefix for the ECR repository namespace, which has a length limit.
  EOT
  type        = string
  default     = "sentinel-sre-demo"

  validation {
    # ECR repository names must be lowercase, and Name tags read badly with
    # anything exotic in them. Catch it here rather than 30 resources in.
    condition     = can(regex("^[a-z][a-z0-9-]{2,30}$", var.project_name))
    error_message = "project_name must be lowercase alphanumeric plus hyphens, 3-31 chars, starting with a letter."
  }
}

# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

variable "vpc_cidr" {
  description = <<-EOT
    CIDR for the dedicated VPC. A brand-new VPC is always created — this
    stack never attaches to a default VPC or an existing one, so it cannot
    accidentally modify networking that something else depends on.
  EOT
  type        = string
  default     = "10.20.0.0/16"
}

variable "public_subnet_cidr" {
  description = "CIDR for the single public subnet the EC2 instance sits in."
  type        = string
  default     = "10.20.1.0/24"
}

variable "availability_zone" {
  description = <<-EOT
    AZ for the public subnet. Leave null to let Terraform pick the first
    available AZ in the region. Pinning it matters only because the EBS
    volume is AZ-bound: if you ever recreate the instance, it has to come
    back in the same AZ to reattach a pre-existing volume.
  EOT
  type        = string
  default     = null
}

variable "allowed_http_cidrs" {
  description = <<-EOT
    Who may reach the public HTTP(S) entrypoint (Traefik on :80/:443).

    Defaults to the whole internet because the acceptance criteria for this
    phase require the portal to be publicly reachable. If you would rather
    keep the demo private, set this to ["<your-ip>/32"] — everything else
    (SSM administration, Sentinel, the K3s API) keeps working, because none
    of it depends on this rule.
  EOT
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "enable_https_ingress" {
  description = <<-EOT
    Open :443 in addition to :80. Off by default: nothing in this
    environment terminates TLS yet (no ACM certificate, no cert-manager,
    no DNS name to issue a certificate for), so an open 443 would just be
    a port that refuses connections. Turn it on when TLS is actually
    configured in Traefik.
  EOT
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# Compute
# ---------------------------------------------------------------------------

variable "instance_type" {
  description = <<-EOT
    EC2 instance type. t3a.large (2 vCPU / 8 GiB) is the deliberate choice
    for this phase: the full workload (3 app services, 2 Postgres,
    Prometheus, Loki, Grafana, Alertmanager, Alloy, Sentinel) requests
    roughly 1.3 vCPU and 2.5 GiB, which fits a 2 vCPU / 8 GiB node with
    burst headroom for the chaos scenarios that deliberately spike CPU.

    Do not drop below 2 vCPU / 4 GiB — K3s plus this stack will not fit.
  EOT
  type        = string
  default     = "m7i-flex.large"
}

variable "root_volume_size_gb" {
  description = <<-EOT
    Size of the gp3 root volume. This single volume holds the OS, all
    container images pulled from ECR, and every PersistentVolume — K3s's
    local-path provisioner writes PVs to /var/lib/rancher/k3s/storage on
    this disk (see the storage note in docs/aws-deployment.md). 40 GiB
    leaves comfortable room for image churn across redeploys.
  EOT
  type        = number
  default     = 40

  validation {
    condition     = var.root_volume_size_gb >= 20
    error_message = "root_volume_size_gb must be at least 20 GiB to hold the OS, container images, and PersistentVolumes."
  }
}

variable "k3s_version" {
  description = <<-EOT
    K3s version channel or exact version to install. Pinning an exact
    version (e.g. "v1.31.4+k3s1") makes the environment reproducible;
    "stable" always installs whatever is current, which is convenient but
    means two applies weeks apart can produce different Kubernetes
    versions.
  EOT
  type        = string
  default     = "v1.31.4+k3s1"
}

variable "app_namespace" {
  description = <<-EOT
    Kubernetes namespace the portal, the observability stack and Sentinel
    all run in. Matches k8s/namespace.yaml. Changing it here without
    changing the manifests will not work — it is passed into the node's
    bootstrap so the ECR pull secret lands in the right namespace.
  EOT
  type        = string
  default     = "citizen-portal"
}

variable "repo_url" {
  description = <<-EOT
    HTTPS clone URL for this repository, cloned onto the node at
    /opt/sentinel-sre so manifests can be applied from the box itself
    (SSM has a payload size limit that makes shipping rendered manifests
    over it impractical).

    Only works unattended for a PUBLIC repository. If the repo is private
    the clone fails harmlessly during bootstrap and the directory has to be
    populated by hand once — see docs/aws-deployment.md. Leave null to skip
    cloning entirely.
  EOT
  type        = string
  default     = null
}

# ---------------------------------------------------------------------------
# ECR
# ---------------------------------------------------------------------------

variable "ecr_repository_names" {
  description = <<-EOT
    Service names to create ECR repositories for. Each becomes
    "<project_name>/<name>", e.g. "sentinel-sre-demo/citizen-service".
  EOT
  type        = list(string)
  default = [
    "citizen-service",
    "notification-service",
    "frontend",
    "sentinel-ai",
  ]
}

variable "ecr_image_tag_mutability" {
  description = <<-EOT
    MUTABLE or IMMUTABLE.

    This defaults to MUTABLE deliberately, and the reason is worth stating
    plainly rather than hiding: CI pushes two tags per build, the immutable
    git SHA (`citizen-service:abc1234`) and a moving `latest`. Kubernetes
    only ever deploys the SHA tag — that is what makes a rollback to a
    specific previous image meaningful, and what lets Sentinel correlate a
    running ReplicaSet back to a commit. `latest` exists purely as a
    human convenience for `docker pull`.

    IMMUTABLE would reject the second push of `latest` and fail the build.
    If you want IMMUTABLE, also drop the `latest` tag from
    .github/workflows/ci-cd.yml — the SHA tags are the ones that matter.
  EOT
  type        = string
  default     = "MUTABLE"

  validation {
    condition     = contains(["MUTABLE", "IMMUTABLE"], var.ecr_image_tag_mutability)
    error_message = "ecr_image_tag_mutability must be MUTABLE or IMMUTABLE."
  }
}

variable "ecr_untagged_image_expiry_days" {
  description = <<-EOT
    Delete untagged ECR images older than this many days. Untagged images
    accumulate on every rebuild of the same tag and are pure storage cost
    with no rollback value (nothing can reference them). Tagged images are
    never expired by this stack — those are the rollback targets.
  EOT
  type        = number
  default     = 14
}

# ---------------------------------------------------------------------------
# GitHub Actions OIDC
# ---------------------------------------------------------------------------

variable "github_repository" {
  description = <<-EOT
    The GitHub repository allowed to assume the deploy role, as
    "owner/repo". This is the *only* thing standing between "our CI can
    push to our ECR" and "any GitHub Actions workflow on the internet
    can", so it must be exact — the trust policy is scoped to this value.

    Leave as null to skip creating the GitHub Actions role entirely (useful
    for a first apply where you just want the infrastructure up and will
    push images by hand from your laptop).
  EOT
  type        = string
  default     = null

  validation {
    condition     = var.github_repository == null || can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "github_repository must be in \"owner/repo\" form, or null to skip creating the CI role."
  }
}

variable "github_allowed_branches" {
  description = <<-EOT
    Branches whose workflow runs may assume the deploy role. Restricting
    this matters: without it, a pull request from a fork could run with
    permission to push images and deploy. Defaults to main only.
  EOT
  type        = list(string)
  default     = ["main"]
}

variable "create_github_oidc_provider" {
  description = <<-EOT
    Whether to create the account-level IAM OIDC provider for GitHub
    Actions.

    Set this to false if the provider for token.actions.githubusercontent.com
    already exists in this AWS account — it is an account-wide singleton, so
    a second `apply` (or another project that already created one) would
    fail with EntityAlreadyExists. Checking first avoids that:

      aws iam list-open-id-connect-providers

    When false, the existing provider is looked up and reused, and this
    stack does not manage its lifecycle — so destroying this stack will
    not remove a provider other projects may depend on.
  EOT
  type        = bool
  default     = true
}
