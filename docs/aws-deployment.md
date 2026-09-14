# AWS Deployment — Runbook

An ordered, runnable procedure for standing up this project on AWS: one EC2 instance running
K3s, with the citizen portal, the observability stack and Sentinel all inside that one cluster.

Thirty-one numbered steps, grouped into eight parts. Every step that verifies something gives a
real command and what its output should look like. Nothing here is a summary of a design — it is
the thing you actually type.

---

## Status: what has and has not been executed

This matters more than anything else in the document, so it goes first.

- The Terraform in `infra/terraform/` is `terraform fmt`-clean and `terraform validate`-clean.
- **`terraform plan` has NOT been run.** `plan` requires live AWS credentials, and none have been
  used against this configuration.
- **No step past step 11 has been executed.** Nothing in this repository has ever been applied to
  a real AWS account. There is no instance, no VPC, no ECR repository, no image in a registry.
- Consequently, every "expected output" block below is derived from the manifests, scripts and
  Terraform in this repository — not transcribed from a run. Treat them as what the code says
  should happen, not as evidence that it did.

You are the first person to run this. Expect to hit something the code did not anticipate; the
troubleshooting section at the end covers the failure modes the code makes likely, which is not
the same as covering all of them.

---

## This is a demonstration environment, not a production government deployment

Read this before deciding what to do with it.

The environment is deliberately a single EC2 instance with a single-node K3s cluster, in-cluster
PostgreSQL on a local disk, plain HTTP on a raw IP address, and local Terraform state. That is a
reasonable shape for demonstrating an autonomous SRE agent to a room of people. It is not a
reasonable shape for anything holding real citizen data.

What a production build of the same system would need, none of which is here:

| Gap | What production needs |
|---|---|
| Single AZ, single instance | Multi-AZ, more than one node, a real HA control plane (three servers with an embedded or external datastore, or a managed control plane) |
| PostgreSQL pods on a local disk | A managed database (RDS/Aurora) with automated backups, point-in-time recovery, and a **tested** restore procedure |
| Plain HTTP on `http://<public-ip>` | A DNS name, a certificate (ACM or cert-manager), TLS terminated before the application, HSTS |
| Nothing filtering requests | A WAF in front of the entrypoint, and rate limiting |
| Instance in a public subnet | Private subnets, with a NAT Gateway or VPC endpoints for outbound |
| Wide-open egress | VPC endpoints for SSM and ECR, so the node needs no route to the internet at all |
| Local `terraform.tfstate` | Remote state in S3 with locking, so the state is not one laptop away from being lost |
| One environment | Separated dev/staging/prod, ideally separate AWS accounts |

Each of those is a deliberate omission with a stated reason, not an oversight. The reasons are in
the Terraform comments and repeated where relevant below.

---

## What was actually built

```
                    Internet
                       |
                       | :80  (security group: only this port is open)
                       v
        +---------------------------------------------+
        |  EC2 t3a.large, public subnet, public IPv4  |
        |  Ubuntu 24.04, 40 GiB encrypted gp3         |
        |                                             |
        |  K3s v1.31.4+k3s1  (single node:            |
        |     control-plane + worker)                 |
        |                                             |
        |   ServiceLB -> Traefik (K3s bundled) :80    |
        |                    |                        |
        |          +---------+---------+              |
        |          | /api              | /            |
        |          v                   v              |
        |   citizen-service:8000   frontend:3000      |
        |          |                                  |
        |          v                                  |
        |   notification-service:8000                 |
        |                                             |
        |   citizen-postgres        notification-     |
        |   (Deployment + PVC)      postgres          |
        |                           (Deployment+PVC)  |
        |                                             |
        |   Prometheus  Loki  Grafana  Alertmanager   |
        |   Alloy (DaemonSet)                         |
        |                                             |
        |   sentinel-ai:8080                          |
        |     <- Alertmanager webhook                 |
        |     -> Kubernetes API (namespaced Role)     |
        +---------------------------------------------+
                       ^
                       | outbound only
                       v
        SSM Session Manager  |  ECR  |  apt / get.k3s.io / Docker Hub
```

`notification-service` has no Ingress rule. It is reached only by `citizen-service` over the
cluster network — the same server-to-server boundary the project has had since the services were
split.

### AWS services used

VPC, Internet Gateway, one public subnet, one route table, one security group, EC2, EBS, IAM,
ECR, Systems Manager (SSM).

### AWS services deliberately not used

EKS, RDS, Application Load Balancer, NAT Gateway, ElastiCache, OpenSearch, MSK, and any
arrangement involving more than one instance.

The reasoning is uniform and it is cost. EKS is a fixed monthly charge for the control plane
before a single node exists; a NAT Gateway is a fixed hourly charge plus data processing. Either
one, on its own, would cost more than the instance that runs the entire environment. For a
demonstration whose whole point is the agent's behaviour rather than the substrate's elasticity,
paying more for the substrate than for the thing being demonstrated is the wrong trade. An ALB
would add a third fixed charge to terminate traffic that Traefik already terminates for free on
the node's own port 80.

The consequences are real and are not hidden anywhere in this document: no redundancy, no managed
backups, no TLS, and an instance that is publicly addressable rather than tucked behind private
subnets. See the security posture and cost sections.

---

## Layout of what you will be running

```
infra/terraform/
  versions.tf          provider + required_version; explains why state is local
  variables.tf         every knob, each with the reasoning in its description
  network.tf           VPC, IGW, public subnet, route table
  security.tf          the security group — and a long comment on what is absent
  iam.tf               instance role: SSM core + ECR pull only
  ecr.tf               four repositories, scan on push, untagged expiry
  ec2.tf               the single instance
  github_oidc.tf       OIDC provider + the role GitHub Actions assumes
  outputs.tf           public_ip, instance_id, github_actions_role_arn, ...
  user_data.sh.tftpl   first-boot bootstrap (K3s, SSM, ECR refresh, deploy helper)

k8s/base/              cluster-agnostic manifests, shared by both environments
k8s/overlays/local/    local development: ingress-nginx, committed demo secrets
k8s/overlays/aws/      this environment: Traefik, ECR images, injected secrets, Sentinel

scripts/generate-aws-secrets.sh   run once on the node, before the first deploy
scripts/deploy-aws.sh             run on the node, per deploy
scripts/smoke-test.sh             end-to-end functional check through the Ingress
scripts/incident-scenarios.sh     the nine chaos scenarios
```

Local development is untouched by any of this. `kubectl apply -k k8s/` still works exactly as it
did against kind, minikube or Docker Desktop. The AWS overlay adds to the base; it does not
change it.

---

## Prerequisites

- An AWS account you are allowed to create IAM roles and a VPC in.
- A machine with a shell. Windows works — use Git Bash or WSL for the `bash`-flavoured commands.
- Terraform 1.5 or newer, AWS CLI v2, and the AWS CLI **Session Manager plugin**.
- Docker, if you intend to build and push images by hand rather than letting CI do it.
- Roughly 40 minutes end to end, most of it waiting.

---

# Part 1 — AWS account preparation (steps 1–4)

## Step 1. Prepare the AWS account

If you already have an account you are comfortable creating a VPC in, skip to step 2.

Otherwise create one at <https://portal.aws.amazon.com/billing/signup>. You will need a payment
method; this environment is not free.

Then decide, before anything else, which region you are deploying to. Everything lives in one
region and moving a running environment between regions means recreating it.

```bash
aws ec2 describe-regions --query 'Regions[].RegionName' --output table
```

The default in `variables.tf` is `eu-west-1`. Change it by setting `aws_region`.

> **t3a is not available in every region.** `t3a.large` is the AMD-based burstable type this
> stack defaults to, and the Gulf regions (`me-central-1`, `me-south-1`) in particular may not
> offer it. If `terraform apply` fails with an unsupported-instance-type error, that is why — set
> `instance_type` to `t3.large` or `m6i.large` and re-apply. Check before you start:
>
> ```bash
> aws ec2 describe-instance-type-offerings \
>   --location-type availability-zone \
>   --filters Name=instance-type,Values=t3a.large \
>   --region eu-west-1 \
>   --query 'InstanceTypeOfferings[].Location' --output table
> ```
>
> An empty result means the type is not offered there.

## Step 2. Enable MFA on the root user

Do this before creating anything. The root user can close the account, change the payment
method, and remove every guardrail you are about to put in place; a password is not sufficient
protection for that.

AWS Console → click your account name (top right) → **Security credentials** → under
**Multi-factor authentication (MFA)** → **Assign MFA device**. A phone authenticator app is
fine. A hardware key is better.

Then stop using the root user. Everything from step 4 onward uses a separate identity.

Verify:

```bash
aws iam get-account-summary --query 'SummaryMap.AccountMFAEnabled'
```

Expected output:

```
1
```

`0` means root MFA is not enabled. Go back and enable it.

## Step 3. Set a billing budget

This environment bills by the hour whether or not anyone is looking at it. A budget does not stop
spending, but it does mean you find out within a day instead of at the end of the month.

Console → **Billing and Cost Management** → **Budgets** → **Create budget** → *Use a template* →
**Monthly cost budget**. Set an amount you would be annoyed but not alarmed by, and an email
address.

Or from the CLI, writing the budget definition to a file first:

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

cat > /tmp/budget.json <<'EOF'
{
  "BudgetName": "sentinel-sre-demo-monthly",
  "BudgetType": "COST",
  "TimeUnit": "MONTHLY",
  "BudgetLimit": { "Amount": "50", "Unit": "USD" }
}
EOF

cat > /tmp/notifications.json <<'EOF'
[
  {
    "Notification": {
      "NotificationType": "ACTUAL",
      "ComparisonOperator": "GREATER_THAN",
      "Threshold": 80,
      "ThresholdType": "PERCENTAGE"
    },
    "Subscribers": [
      { "SubscriptionType": "EMAIL", "Address": "you@example.com" }
    ]
  }
]
EOF

aws budgets create-budget \
  --account-id "$ACCOUNT_ID" \
  --budget file:///tmp/budget.json \
  --notifications-with-subscribers file:///tmp/notifications.json
```

The `50` and the email address are placeholders — set them to your own values. The amount is not
an estimate of what this costs; see the cost section for why no figure is quoted.

Verify:

```bash
aws budgets describe-budgets --account-id "$ACCOUNT_ID" \
  --query 'Budgets[].{Name:BudgetName,Limit:BudgetLimit.Amount}' --output table
```

## Step 4. Create an administrative identity (IAM Identity Center, or an IAM user)

Terraform needs to create IAM roles, a VPC, EC2 and ECR resources. That is broad enough that the
practical answer for a single-operator demo account is an administrative identity — but not the
root user.

**Preferred: IAM Identity Center.** It issues short-lived credentials, so there is no long-lived
secret on your laptop to leak.

1. Console → **IAM Identity Center** → **Enable**.
2. **Users** → **Add user**. Give it your email.
3. **Permission sets** → **Create permission set** → *Predefined* → **AdministratorAccess**.
4. **AWS accounts** → select your account → **Assign users or groups** → pick the user and the
   permission set.
5. Note the **AWS access portal URL** shown on the Identity Center dashboard. You need it in
   step 6.

**Alternative: an IAM user.** Only if Identity Center is unavailable to you.

Console → **IAM** → **Users** → **Create user** → attach **AdministratorAccess** →
**Security credentials** → **Create access key** → *Command Line Interface*. Enable MFA on this
user too.

> **A note on scope.** `AdministratorAccess` is more than this stack strictly needs, and in a
> shared account you should scope it down to the services in `infra/terraform/` (EC2, VPC, IAM,
> ECR, SSM). In a personal demo account the cost of getting that policy wrong — a failed apply
> halfway through, with resources half-created — is higher than the benefit. Decide which
> situation you are in.

Whichever you chose, **do not create an AWS access key for GitHub Actions.** CI authenticates
through OIDC federation and needs no key at all. See `docs/github-configuration.md`.

---

# Part 2 — Local tooling (steps 5–7)

## Step 5. Install the AWS CLI (v2) and the Session Manager plugin

Both are needed. The CLI provisions and inspects; the Session Manager plugin is what makes
`aws ssm start-session` work, and without it you have **no way to administer the instance at
all** — there is no SSH port open and no key pair.

macOS:

```bash
brew install awscli
brew install --cask session-manager-plugin
```

Linux (x86_64):

```bash
curl -sSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
unzip -q awscliv2.zip && sudo ./aws/install

curl -sSL "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/ubuntu_64bit/session-manager-plugin.deb" \
  -o session-manager-plugin.deb
sudo dpkg -i session-manager-plugin.deb
```

Windows: download and run the AWS CLI MSI and the Session Manager plugin installer from AWS's
documented URLs.

Verify:

```bash
aws --version
session-manager-plugin --version
```

Expected output — versions will differ, the shape will not:

```
aws-cli/2.17.42 Python/3.11.9 Linux/6.8.0 exe/x86_64
1.2.633.0
```

If `session-manager-plugin` is not found, stop and fix it now rather than at step 13.

## Step 6. Authenticate the AWS CLI

**With IAM Identity Center:**

```bash
aws configure sso
```

It prompts for the access portal URL from step 4, a region, and an account/role. Give the
profile a name — `sentinel-demo` below. Then:

```bash
aws sso login --profile sentinel-demo
export AWS_PROFILE=sentinel-demo
```

**With an IAM user:**

```bash
aws configure
```

Paste the access key id and secret, set the region to the one you chose in step 1, and set output
to `json`.

Verify, either way:

```bash
aws sts get-caller-identity
```

Expected output:

```json
{
    "UserId": "AROA...:you@example.com",
    "Account": "123456789012",
    "Arn": "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_AdministratorAccess_.../you@example.com"
}
```

The `Account` value is your account id. Note it — several later commands use it, and it appears
in the ECR registry hostname.

If this fails with `ExpiredToken`, run `aws sso login` again. SSO sessions are short by design.

## Step 7. Install Terraform

Version 1.5.0 or newer (`versions.tf` requires it).

```bash
# macOS
brew tap hashicorp/tap && brew install hashicorp/tap/terraform

# Debian/Ubuntu
wget -O- https://apt.releases.hashicorp.com/gpg \
  | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] \
https://apt.releases.hashicorp.com $(lsb_release -cs) main" \
  | sudo tee /etc/apt/sources.list.d/hashicorp.list
sudo apt update && sudo apt install terraform
```

Verify:

```bash
terraform version
```

Expected output:

```
Terraform v1.9.5
on linux_amd64
```

---

# Part 3 — Provision the infrastructure (steps 8–11)

Everything in this part runs from `infra/terraform/`.

```bash
cd infra/terraform
```

### Before you start: state is local, and that has consequences

`versions.tf` declares no backend. State lands in `infra/terraform/terraform.tfstate` on the
machine that runs `apply`, and it is gitignored.

That is a deliberate choice for a single-operator demo: a remote backend means creating an S3
bucket and a lock table *before* the environment they are meant to protect, which is more AWS
surface and more cost than this phase wants, and it introduces a bootstrapping problem for a
stack whose entire point is that it is small.

The consequence is not optional to understand:

> **If you lose `terraform.tfstate`, Terraform loses track of these resources.** They keep
> running and keep billing. Terraform simply no longer knows they exist, so `plan` will propose
> creating a second copy of everything and `destroy` will remove nothing. Recovery means
> importing each resource by hand.

Back it up somewhere. After a successful apply:

```bash
cp terraform.tfstate ~/secure-backups/sentinel-sre-$(date +%F).tfstate
```

The state file contains no AWS access keys (there are none in this design) but it does contain
resource ids and the rendered user-data. Treat it as sensitive.

## Step 8. `terraform init`

```bash
terraform init
```

Expected output ends with:

```
Initializing the backend...
Initializing provider plugins...
- Finding hashicorp/aws versions matching "~> 5.60"...
- Installing hashicorp/aws v5.70.0...
- Installed hashicorp/aws v5.70.0 (signed by HashiCorp)

Terraform has been successfully initialized!
```

This downloads the AWS provider into `.terraform/` and writes `.terraform.lock.hcl`. It contacts
no AWS API and creates nothing.

## Step 9. `terraform validate`

```bash
terraform fmt -check -recursive
terraform validate
```

Expected output — `fmt -check` prints nothing when every file is already formatted, and
`validate` prints:

```
Success! The configuration is valid.
```

This is syntax and type checking against the provider schema. It needs no credentials, which is
exactly why it is the last thing that has actually been run against this configuration.

## Step 10. `terraform plan`

Create a `terraform.tfvars` first. Only `github_repository` really wants setting; everything else
has a defensible default.

```bash
cat > terraform.tfvars <<'EOF'
aws_region        = "eu-west-1"
github_repository = "your-org/Sentinel-SRE"
repo_url          = "https://github.com/your-org/Sentinel-SRE.git"
EOF
```

- `github_repository` scopes the OIDC trust policy. It is the only thing standing between "our CI
  can push to our ECR" and "any GitHub Actions workflow on the internet can", so it must be
  exact. Leave it unset (`null`) to skip creating the CI role entirely and push images by hand.
- `repo_url` is cloned onto the node at `/opt/sentinel-sre` during bootstrap so you can run
  `deploy-aws.sh` from the box. It only works unattended for a **public** repository; for a
  private one the clone fails harmlessly and you populate the directory yourself (see
  troubleshooting).
- If the account already has an OIDC provider for `token.actions.githubusercontent.com`, add
  `create_github_oidc_provider = false`. Check first:

  ```bash
  aws iam list-open-id-connect-providers
  ```

  A non-empty result mentioning `token.actions.githubusercontent.com` means one exists. It is an
  account-level singleton; creating a second fails with `EntityAlreadyExists`.

Then:

```bash
terraform plan -out=tfplan
```

Expected output ends with something close to:

```
Plan: 16 to add, 0 to change, 0 to destroy.

Changes to Outputs:
  + aws_region              = "eu-west-1"
  + ecr_registry            = (known after apply)
  + ecr_repository_urls     = (known after apply)
  + github_actions_role_arn = (known after apply)
  + instance_id             = (known after apply)
  + portal_url              = (known after apply)
  + public_ip               = (known after apply)
  + ssm_session_command     = (known after apply)
  + vpc_id                  = (known after apply)
  + next_steps              = (known after apply)
```

The exact resource count depends on `allowed_http_cidrs` (one ingress rule per CIDR),
`enable_https_ingress`, `create_github_oidc_provider`, and whether `github_repository` is set.

**Read the plan.** Specifically confirm:

- `0 to destroy`. This configuration only creates; a destroy in a first plan means you are
  pointed at state that already has resources in it.
- `aws_vpc.main` is being **created**, not modified. This stack never attaches to a default VPC
  or an existing one, so it cannot break networking something else depends on.
- The security group has ingress on **80 only** (plus 443 if you enabled it) and **no 22**.
- No resource anywhere contains an access key.

## Step 11. `terraform apply`

```bash
terraform apply tfplan
```

Applying a saved plan means what you reviewed is exactly what runs — no second confirmation
prompt, and no chance of the world having changed between plan and apply in a way that silently
alters the outcome.

Expected tail:

```
aws_instance.k3s: Creating...
aws_instance.k3s: Still creating... [30s elapsed]
aws_instance.k3s: Creation complete after 42s [id=i-0123456789abcdef0]

Apply complete! Resources: 16 added, 0 changed, 0 destroyed.

Outputs:

aws_region = "eu-west-1"
ecr_registry = "123456789012.dkr.ecr.eu-west-1.amazonaws.com"
github_actions_role_arn = "arn:aws:iam::123456789012:role/sentinel-sre-demo-github-actions"
instance_id = "i-0123456789abcdef0"
portal_url = "http://52.30.11.204"
public_ip = "52.30.11.204"
ssm_session_command = "aws ssm start-session --target i-0123456789abcdef0 --region eu-west-1"
vpc_id = "vpc-0abc123def456789a"
next_steps = <<EOT
Terraform has created the infrastructure. The environment is NOT yet
running the application. Next:
...
EOT
```

Save the outputs — you need three of them repeatedly:

```bash
export INSTANCE_ID=$(terraform output -raw instance_id)
export PUBLIC_IP=$(terraform output -raw public_ip)
export AWS_REGION=$(terraform output -raw aws_region)
export ECR_REGISTRY=$(terraform output -raw ecr_registry)
```

`apply` finishing does **not** mean the environment is ready. The instance has only just started
running its bootstrap; K3s is not installed yet, and nothing of the application exists. That is
what the `next_steps` output says, and it is deliberately worded not to claim success.

### Two lifecycle rules you will eventually run into

`ec2.tf` puts two guards on the instance, and both will surprise you if you do not know they are
there.

**`prevent_destroy = true`.** `terraform destroy` will refuse:

```
Error: Instance cannot be destroyed

  on ec2.tf line 33:
  33: resource "aws_instance" "k3s" {

Resource aws_instance.k3s has lifecycle.prevent_destroy set, but the plan
calls for this resource to be destroyed.
```

This is intentional. The instance's root volume holds every PersistentVolume in the cluster —
both databases, Sentinel's incident history, Prometheus and Loki data. An accidental `destroy`
would take all of it. To tear the environment down **on purpose**, edit `ec2.tf`, remove the
`prevent_destroy = true` line, apply that change, and *then* destroy:

```bash
# 1. remove `prevent_destroy = true` from ec2.tf
terraform apply       # applies only the lifecycle-meta change
terraform destroy
```

Note that ECR repositories will also refuse to be destroyed while they still contain images —
`force_delete` is deliberately unset, for the same reason.

**`ignore_changes = [ami]`.** The AMI id comes from Canonical's public SSM parameter, read at
plan time, so it moves whenever Canonical publishes a new build. Without this rule, an unrelated
`apply` weeks later would propose **replacing the instance** — wiping the cluster — purely
because a patched image exists. With it, the AMI drift is ignored and OS upgrades become a
deliberate act: remove the line, apply, and re-bootstrap from scratch.

The tradeoff is honest and worth stating: this means the instance does **not** pick up new base
images automatically. Patching the running OS is `apt upgrade` over SSM, not a Terraform
operation.

---

# Part 4 â€” Verify the infrastructure (steps 12â€“15)

## Step 12. Verify the EC2 instance

```bash
aws ec2 describe-instances --instance-ids "$INSTANCE_ID" --region "$AWS_REGION" \
  --query 'Reservations[].Instances[].{
    State:State.Name,
    Type:InstanceType,
    AZ:Placement.AvailabilityZone,
    PublicIP:PublicIpAddress,
    Profile:IamInstanceProfile.Arn,
    IMDSv2:MetadataOptions.HttpTokens,
    KeyName:KeyName
  }' --output table
```

Expected output:

```
------------------------------------------------------------------------
|                           DescribeInstances                          |
+------------+------------+-------------+--------------+---------------+
|     AZ     |  IMDSv2    |  PublicIP   |    State     |     Type      |
+------------+------------+-------------+--------------+---------------+
|eu-west-1a  |  required  |52.30.11.204 |   running    |  t3a.large    |
+------------+------------+-------------+--------------+---------------+
```

Three things to confirm specifically:

- `State` is `running`.
- `IMDSv2` is `required`. Not `optional`. The instance profile's credentials are served by IMDS,
  so mandatory tokens are what closes the SSRF-to-credential-theft path that IMDSv1 leaves open â€”
  which matters here because this node runs web-facing workloads.
- `KeyName` is **absent from the output entirely** (or `null`). There is no SSH key pair. If a key
  name appears, something has been changed from what this repository declares.

Confirm the root volume is encrypted gp3:

```bash
aws ec2 describe-volumes --region "$AWS_REGION" \
  --filters "Name=attachment.instance-id,Values=$INSTANCE_ID" \
  --query 'Volumes[].{Size:Size,Type:VolumeType,Encrypted:Encrypted,State:State}' \
  --output table
```

Expected:

```
--------------------------------------------
|              DescribeVolumes             |
+-----------+--------+--------+------------+
| Encrypted | Size   | State  |   Type     |
+-----------+--------+--------+------------+
|  True     |  40    | in-use |   gp3      |
+-----------+--------+--------+------------+
```

Encryption uses the AWS-managed EBS key. It is free, and because K3s's local-path provisioner
writes every PersistentVolume onto this disk, it means the PostgreSQL data files are encrypted at
rest without any extra configuration.

## Step 13. Verify SSM registration and open a session

This is the step that determines whether you can administer the box at all. There is no fallback.

```bash
aws ssm describe-instance-information --region "$AWS_REGION" \
  --filters "Key=InstanceIds,Values=$INSTANCE_ID" \
  --query 'InstanceInformationList[].{Id:InstanceId,Ping:PingStatus,Platform:PlatformName,Agent:AgentVersion}' \
  --output table
```

Expected output:

```
------------------------------------------------------------------
|                  DescribeInstanceInformation                   |
+----------+------------------------+---------+------------------+
|  Agent   |          Id            |  Ping   |    Platform      |
+----------+------------------------+---------+------------------+
| 3.3.1611 | i-0123456789abcdef0    | Online  |  Ubuntu          |
+----------+------------------------+---------+------------------+
```

An **empty list** means the instance has not registered. Give it two or three minutes after
`apply` and try again; if it stays empty, see "SSM agent not registering" in troubleshooting.

Open a shell:

```bash
aws ssm start-session --target "$INSTANCE_ID" --region "$AWS_REGION"
```

Expected:

```
Starting session with SessionId: you@example.com-0abc123def456789a
$
```

You are `ssm-user`, a non-root account. Everything cluster-related needs `sudo`, because the
kubeconfig at `/etc/rancher/k3s/k3s.yaml` is mode `0600` and owned by root.

> **How this works, and why there is no inbound rule for it.** Session Manager is an *outbound*
> connection from the instance to the SSM service, established by the agent using the instance
> profile. Nothing connects *to* the instance. That is precisely why it is used here: there is no
> SSH port to brute-force and no private key anywhere to lose.

Confirm the bootstrap finished. Still inside the session:

```bash
sudo cat /var/lib/sentinel/bootstrap-complete
```

Expected output:

```
completed_at=2026-08-21T09:14:02+00:00
k3s_version=v1.31.4+k3s1
region=eu-west-1
ecr_registry=123456789012.dkr.ecr.eu-west-1.amazonaws.com
namespace=citizen-portal
```

`No such file or directory` means the bootstrap did not complete. It takes roughly 3â€“5 minutes
from instance launch. Watch it:

```bash
sudo tail -f /var/log/sentinel-bootstrap.log
```

The log is written by `user_data.sh.tftpl` and ends with
`=== sentinel bootstrap complete: ... ===` on success. If it ends anywhere else, the last lines
tell you which of the eight bootstrap stages failed.

## Step 14. Verify K3s

Inside the SSM session:

```bash
sudo kubectl get nodes -o wide
```

Expected output:

```
NAME                STATUS   ROLES                  AGE   VERSION        INTERNAL-IP   OS-IMAGE
sentinel-k3s-node   Ready    control-plane,master   6m    v1.31.4+k3s1   10.20.1.87    Ubuntu 24.04.1 LTS
```

One node, `Ready`, with roles `control-plane,master` â€” it is also the worker, because a K3s server
runs an agent unless told not to. The version must match `k3s_version` from `variables.tf`
(`v1.31.4+k3s1` by default). Pinning an exact version rather than `stable` is what makes two
applies weeks apart produce the same Kubernetes version.

Check the system namespace:

```bash
sudo kubectl -n kube-system get pods
```

Expected:

```
NAME                                      READY   STATUS      RESTARTS   AGE
coredns-...                               1/1     Running     0          6m
local-path-provisioner-...                1/1     Running     0          6m
metrics-server-...                        1/1     Running     0          6m
helm-install-traefik-crd-...              0/1     Completed   0          6m
helm-install-traefik-...                  0/1     Completed   0          6m
svclb-traefik-...                         2/2     Running     0          5m
traefik-...                               1/1     Running     0          5m
```

The two `helm-install-traefik*` pods showing `Completed` is correct, not a failure â€” K3s installs
Traefik asynchronously through a HelmChart custom resource, and those Jobs exit when done.

Confirm the application namespace exists (bootstrap creates it, so the ECR pull secret has
somewhere to live before the first deploy):

```bash
sudo kubectl get ns citizen-portal
```

Expected:

```
NAME             STATUS   AGE
citizen-portal   Active   6m
```

And confirm the ECR credential refresh timer is installed and armed:

```bash
sudo systemctl list-timers refresh-ecr-credentials.timer --all
sudo kubectl -n citizen-portal get secret ecr-credentials
```

Expected:

```
NEXT                          LEFT      LAST                          PASSED   UNIT
Fri 2026-08-21 15:16:02 UTC   5h 59min  Fri 2026-08-21 09:16:02 UTC   1min ago refresh-ecr-credentials.timer

NAME              TYPE                             DATA   AGE
ecr-credentials   kubernetes.io/dockerconfigjson   1      5m
```

> **Why a timer.** ECR authorization tokens are valid for 12 hours, so a static `imagePullSecret`
> written once at provisioning time would break every night. The timer rewrites it every 6 hours
> and on boot, authenticating with the instance profile â€” no stored credentials. The alternative,
> `/etc/rancher/k3s/registries.yaml`, would need a K3s restart on every refresh; an
> `imagePullSecret` does not.

### Traefik stays, ingress-nginx must not be installed

The bootstrap deliberately does **not** pass `--disable=traefik`. K3s's bundled Traefik is the
ingress controller for this environment, and its ServiceLB (klipper-lb) is what binds Traefik's
LoadBalancer Service to the node's `:80` â€” which is what makes the instance's public IP serve the
portal with no cloud load balancer involved at all.

Do not install ingress-nginx alongside it. Two controllers both claiming the node's port 80 is a
hostPort conflict, and the second one to start will sit unschedulable. The AWS overlay's Ingress
sets `ingressClassName: traefik` for exactly this reason; the local overlay is the one that uses
nginx.

## Step 15. Verify ECR

Back on your own machine (exit the SSM session with `exit`):

```bash
aws ecr describe-repositories --region "$AWS_REGION" \
  --query 'repositories[].{Name:repositoryName,Mutability:imageTagMutability,ScanOnPush:imageScanningConfiguration.scanOnPush}' \
  --output table
```

Expected output:

```
--------------------------------------------------------------------------
|                         DescribeRepositories                           |
+---------------------------------------------+-------------+------------+
|                    Name                     | Mutability  | ScanOnPush |
+---------------------------------------------+-------------+------------+
|  sentinel-sre-demo/citizen-service          |  MUTABLE    |  True      |
|  sentinel-sre-demo/frontend                 |  MUTABLE    |  True      |
|  sentinel-sre-demo/notification-service     |  MUTABLE    |  True      |
|  sentinel-sre-demo/sentinel-ai              |  MUTABLE    |  True      |
+---------------------------------------------+-------------+------------+
```

Four repositories, all empty at this point.

**Scan on push** is enabled because it is free and means a known-vulnerable base image shows up in
the console without enabling Inspector account-wide, which is a per-resource charge.

**`MUTABLE` is deliberate**, and the reason is worth being plain about rather than hiding. CI
pushes two tags per build: the immutable git SHA (`citizen-service:9f3c1a8...`) and a moving
`latest`. Kubernetes only ever deploys the SHA tag â€” that is what makes a rollback to a specific
previous image meaningful, and what lets Sentinel correlate a running ReplicaSet back to a commit.
`latest` exists purely as a human convenience for `docker pull` and is never deployed.

`IMMUTABLE` would reject the second push of `latest` and fail the build. If you want `IMMUTABLE`,
set `ecr_image_tag_mutability = "IMMUTABLE"` *and* remove the `latest` tag from
`.github/workflows/ci-cd.yml`. The SHA tags are the ones that matter.

Confirm the lifecycle policy:

```bash
aws ecr get-lifecycle-policy --region "$AWS_REGION" \
  --repository-name sentinel-sre-demo/citizen-service \
  --query 'lifecyclePolicyText' --output text
```

Expected (formatted for readability):

```json
{
  "rules": [
    {
      "rulePriority": 1,
      "description": "Expire untagged images after 14 days",
      "selection": {
        "tagStatus": "untagged",
        "countType": "sinceImagePushed",
        "countUnit": "days",
        "countNumber": 14
      },
      "action": { "type": "expire" }
    }
  ]
}
```

Untagged images are the layers orphaned every time `latest` is repointed. Nothing can reference
them, so they have no rollback value â€” they are pure storage cost, and 14 days is long enough to
notice if the rule is wrong.

**Tagged images are never expired by this stack, on purpose.** The SHA tags *are* the rollback
targets, and an automatic "keep only the last N tagged images" rule is exactly the kind of thing
that deletes the image you needed thirty seconds before you needed it. The consequence is that
tagged storage grows without bound; prune it deliberately when it matters.

Finally, confirm the node can actually pull. Inside an SSM session:

```bash
sudo aws ecr get-login-password --region "$AWS_REGION" | head -c 20; echo
```

Expected: 20 characters of an opaque token. An `AccessDenied` here means the instance profile's
inline ECR policy is not attached â€” that policy grants `ecr:GetAuthorizationToken` plus the three
layer-read actions, scoped to this project's repositories, and nothing else. No push. No delete.

---

# Part 5 â€” Build and push images (steps 16â€“17)

Two routes exist. Pick one.

**Route A â€” let CI do it.** Configure the GitHub secrets and variables per
`docs/github-configuration.md`, push to `main`, and the pipeline builds all four images, pushes
them to ECR tagged with the commit SHA and `latest`, then triggers the deploy over SSM. This is
the intended path and the one the repository is built around.

**Route B â€” build locally.** Useful for the very first deploy, before CI is wired, and for
debugging. Steps 16 and 17 describe this route.

## Step 16. Build the four images

From the repository root **on your own machine** (not on the node â€” the node has no Docker; K3s
uses containerd and pulls from ECR).

```bash
export AWS_REGION=eu-west-1
export ECR_REGISTRY="$(aws sts get-caller-identity --query Account --output text).dkr.ecr.${AWS_REGION}.amazonaws.com"
export PREFIX=sentinel-sre-demo
export TAG=$(git rev-parse HEAD)
```

Use the real commit SHA as the tag, not `latest`. The SHA is what makes a running ReplicaSet
traceable back to a commit, which is what Sentinel's deployment correlation depends on. `latest`
is fine for a throwaway test and useless for anything else.

```bash
docker build -t "${ECR_REGISTRY}/${PREFIX}/citizen-service:${TAG}"      ./citizen-service
docker build -t "${ECR_REGISTRY}/${PREFIX}/notification-service:${TAG}" ./notification-service
docker build -t "${ECR_REGISTRY}/${PREFIX}/sentinel-ai:${TAG}"          ./sentinel-ai
docker build -t "${ECR_REGISTRY}/${PREFIX}/frontend:${TAG}" \
  --build-arg VITE_API_BASE_URL= ./frontend
```

> **The frontend build argument is not optional and not cosmetic.** `VITE_API_BASE_URL=` (empty)
> makes the browser call same-origin `/api/...` paths, which is what the Ingress routes. The value
> `docker-compose` uses locally is `http://localhost:8000`, and a frontend built with that will
> load fine on the public IP and then fail every API call, because the browser will be trying to
> reach the visitor's own machine. If the portal renders but nothing works, check this first.

Expected: four successful builds. Confirm:

```bash
docker images --filter "reference=${ECR_REGISTRY}/${PREFIX}/*" \
  --format 'table {{.Repository}}\t{{.Tag}}\t{{.Size}}'
```

Expected output:

```
REPOSITORY                                                              TAG        SIZE
123456789012.dkr.ecr.eu-west-1.amazonaws.com/sentinel-sre-demo/frontend             9f3c1a8...  48MB
123456789012.dkr.ecr.eu-west-1.amazonaws.com/sentinel-sre-demo/sentinel-ai          9f3c1a8...  180MB
123456789012.dkr.ecr.eu-west-1.amazonaws.com/sentinel-sre-demo/notification-service 9f3c1a8...  210MB
123456789012.dkr.ecr.eu-west-1.amazonaws.com/sentinel-sre-demo/citizen-service      9f3c1a8...  240MB
```

Sizes will differ. Four images with the same tag is the thing to confirm.

## Step 17. Push to ECR

```bash
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR_REGISTRY"
```

Expected:

```
Login Succeeded
```

Then:

```bash
for svc in citizen-service notification-service frontend sentinel-ai; do
  docker push "${ECR_REGISTRY}/${PREFIX}/${svc}:${TAG}"
done
```

Verify from the registry side, not from your Docker daemon:

```bash
for svc in citizen-service notification-service frontend sentinel-ai; do
  echo "--- $svc"
  aws ecr describe-images --region "$AWS_REGION" \
    --repository-name "${PREFIX}/${svc}" \
    --query 'imageDetails[].{Tags:imageTags,Pushed:imagePushedAt,MB:imageSizeInBytes}' \
    --output table
done
```

Expected, per repository:

```
--- citizen-service
-------------------------------------------------------------------
|                          DescribeImages                         |
+-----------+---------------------------+-------------------------+
|    MB     |          Pushed           |         Tags            |
+-----------+---------------------------+-------------------------+
| 251658240 | 2026-08-21T09:41:18+00:00 |  9f3c1a8...             |
+-----------+---------------------------+-------------------------+
```

If `docker login` succeeded but `push` returns `denied`, the identity you are logged in as does
not have push rights. The *instance profile* has pull-only permissions by design; pushing is a CI
and operator concern, so push with your own administrative identity or through the GitHub Actions
role.

---

# Part 6 â€” Deploy to Kubernetes (steps 18â€“19)

## Step 18. Deploy the application

Everything in this part runs **on the node**, over SSM:

```bash
aws ssm start-session --target "$INSTANCE_ID" --region "$AWS_REGION"
```

### 18a. First deploy only: generate the secrets

The AWS overlay's `secretGenerator` reads `k8s/overlays/aws/secrets/*.env`. Those files are
gitignored and **absent from a fresh checkout** â€” only the `.env.example` templates are committed.

```bash
sudo /opt/sentinel-sre/scripts/generate-aws-secrets.sh
```

Expected output:

```
Generating secrets in /opt/sentinel-sre/k8s/overlays/aws/secrets
  wrote  citizen-postgres.env
  wrote  notification-postgres.env
  wrote  citizen-service.env
  wrote  notification-service.env
  wrote  grafana.env
  wrote  sentinel.env

Done.

Values you may want to note now (they are not printed again):

  Grafana admin password : 8f2a...
  Chaos admin token      : 41c9...
```

**Write down the Grafana password and the chaos admin token now.** They are not printed again, and
you need the chaos token for every scenario in step 28.

> **Why generated on the node rather than passed in from CI.** Every value except Sentinel's
> optional third-party credentials is internal to the cluster: two database passwords, the JWT
> signing key, the chaos admin token, the Grafana admin password. Nothing outside the cluster needs
> to know them, so the fewer places they exist the better. Generating them here means they never
> travel through a GitHub Actions log, an SSM command parameter, or a Terraform state file.

> **Missing `.env` files make `kubectl kustomize` fail loudly, and that is intentional.** Failing
> closed beats defaulting to a known password on an internet-facing host. If you see
> `no such file or directory` from Kustomize naming one of these files, you skipped this step.

Sentinel's own optional credentials go in `sentinel.env`, which the generator leaves blank:

```bash
sudo nano /opt/sentinel-sre/k8s/overlays/aws/secrets/sentinel.env
```

```
OPENAI_API_KEY=          # LLM-enriched root cause narrative
GITHUB_TOKEN=            # automatic incident issues
GITHUB_REPOSITORY=       # owner/repo for those issues
SLACK_WEBHOOK_URL=       # incident notifications
CHAOS_ADMIN_TOKEN=41c9...  # already filled in by the generator
```

All four are optional. With none of them set, Sentinel still detects, investigates, correlates,
decides, remediates and validates â€” it runs rule-based and records in the incident that the RCA
narrative was produced without LLM enrichment. Nothing silently degrades.

Do **not** put these in GitHub Secrets. They are consumed by a pod at runtime, not by CI. See
`docs/github-configuration.md`.

### 18b. Apply the overlay

```bash
sudo /opt/sentinel-sre/scripts/deploy-aws.sh "$TAG"
```

where `$TAG` is the git SHA you pushed in step 17. The script:

1. Discovers the account id from STS and the region and public IP from IMDS (IMDSv2 token dance
   included, because the instance requires it). Nothing is hardcoded.
2. Checks all six secret `.env` files exist, and stops with a useful message if not.
3. Forces an ECR credential refresh, so a deploy after a long idle period does not hit an expired
   12-hour token.
4. Runs `kubectl kustomize k8s/overlays/aws` and substitutes three placeholders in the rendered
   stream â€” nothing on disk is modified:
   - `ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com` â†’ the real registry
   - `:PLACEHOLDER` â†’ `:<git-sha>`
   - `PUBLIC_IP_PLACEHOLDER` â†’ the instance's public IP (for the CORS origin)
5. **Fails if any placeholder survived.** All three are deliberately invalid rather than plausible
   defaults, so a substitution that silently did not happen produces an immediate, obvious error
   instead of quietly running the wrong thing.
6. Applies, waits for the databases first (the app services' init containers run
   `alembic upgrade head` and crash-loop until PostgreSQL accepts connections), then the rest.
7. Reports the actual pod state and exits non-zero if anything is not Running.

Expected output, abbreviated:

```
--- discovering environment ---
  region     : eu-west-1
  account    : 123456789012
  public IP  : 52.30.11.204
  registry   : 123456789012.dkr.ecr.eu-west-1.amazonaws.com
  image tag  : 9f3c1a8...
--- refreshing ECR pull credentials ---
--- rendering overlay ---
--- applying ---
namespace/citizen-portal configured
configmap/citizen-service-config created
secret/citizen-postgres-secret created
...
deployment.apps/sentinel-ai created
ingress.networking.k8s.io/citizen-portal-ingress created
--- waiting for databases ---
deployment "citizen-postgres" successfully rolled out
deployment "notification-postgres" successfully rolled out
--- waiting for application and observability stack ---
deployment "citizen-service" successfully rolled out
...
Applied successfully. All pods are Running.

That is NOT the same as the environment being verified. Still to check:
  curl -sS http://52.30.11.204/
  ...
```

Note the last paragraph. The script deliberately does not claim the environment is verified â€” that
is what steps 19 through 31 are for.

For manifest changes rather than image changes, use the same script; for image-only changes CI
uses the narrower path, `/usr/local/bin/sentinel-deploy.sh images <sha>`, which is all the SSM
policy permits it to invoke.

## Step 19. Verify pods

```bash
sudo kubectl -n citizen-portal get pods -o wide
```

Expected output:

```
NAME                                     READY   STATUS    RESTARTS   AGE    NODE
alertmanager-6c8b9f7d4-x2k9p             1/1     Running   0          3m     sentinel-k3s-node
alloy-7fw2n                              1/1     Running   0          3m     sentinel-k3s-node
citizen-postgres-58d7c9b64-mn4tq         1/1     Running   0          4m     sentinel-k3s-node
citizen-service-7b9d8f5c6-qp8rw          1/1     Running   0          3m     sentinel-k3s-node
frontend-5f7c8d9b4-tk2mz                 1/1     Running   0          3m     sentinel-k3s-node
grafana-6d9f8c7b5-vn3xq                  1/1     Running   0          3m     sentinel-k3s-node
loki-8c7d6f5b9-hj4kp                     1/1     Running   0          3m     sentinel-k3s-node
notification-postgres-6b8c7d9f5-wq2nx    1/1     Running   0          4m     sentinel-k3s-node
notification-service-9d7c8b6f5-rt5mk     1/1     Running   0          3m     sentinel-k3s-node
prometheus-7c9d8b5f6-lm3pq               1/1     Running   0          3m     sentinel-k3s-node
sentinel-ai-8f7c6d5b9-zx4nq              1/1     Running   0          3m     sentinel-k3s-node
```

Eleven pods, all `Running`, all `1/1`, all on the one node. Restart counts should be `0`; a small
number on the app services is plausible on a first deploy if a service came up before its
database finished initialising, and is not itself a problem.

Replica counts are 1 for `citizen-service`, `notification-service` and `frontend` â€” the base runs
them at 2. This is `patch-replicas.yaml`, and it exists for two reasons. The arithmetic one: the
full stack requests roughly 1.5 of the node's 2 vCPU at base replica counts, leaving no room for
the chaos scenarios that deliberately spike CPU. The more important one: **chaos state is
per-pod**, an in-memory singleton in each process, so a fault injected through the Service VIP
lands on exactly one replica. At two replicas a fault affects roughly half of traffic and a reset
may hit the pod that was never faulted â€” which makes Sentinel's recovery validation unreliable in
a way that has nothing to do with Sentinel.

Confirm the deployments and their rollout history depth:

```bash
sudo kubectl -n citizen-portal get deployments
```

Expected:

```
NAME                    READY   UP-TO-DATE   AVAILABLE   AGE
alertmanager            1/1     1            1           3m
citizen-postgres        1/1     1            1           4m
citizen-service         1/1     1            1           3m
frontend                1/1     1            1           3m
grafana                 1/1     1            1           3m
loki                    1/1     1            1           3m
notification-postgres   1/1     1            1           4m
notification-service    1/1     1            1           3m
prometheus              1/1     1            1           3m
sentinel-ai             1/1     1            1           3m
```

And that the images really are ECR images pinned to your SHA â€” not `latest`, and not a
placeholder:

```bash
sudo kubectl -n citizen-portal get deploy -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.containers[0].image}{"\n"}{end}'
```

Expected (abbreviated):

```
citizen-service        123456789012.dkr.ecr.eu-west-1.amazonaws.com/sentinel-sre-demo/citizen-service:9f3c1a8...
frontend               123456789012.dkr.ecr.eu-west-1.amazonaws.com/sentinel-sre-demo/frontend:9f3c1a8...
notification-service   123456789012.dkr.ecr.eu-west-1.amazonaws.com/sentinel-sre-demo/notification-service:9f3c1a8...
sentinel-ai            123456789012.dkr.ecr.eu-west-1.amazonaws.com/sentinel-sre-demo/sentinel-ai:9f3c1a8...
```

Seeing `PLACEHOLDER` or `ACCOUNT_ID.dkr.ecr` anywhere here means the substitution did not happen â€”
see troubleshooting.

## Step 20. Verify Traefik and the public entrypoint

First, that Traefik has actually claimed the node's port 80:

```bash
sudo kubectl -n kube-system get svc traefik
```

Expected:

```
NAME      TYPE           CLUSTER-IP     EXTERNAL-IP    PORT(S)                      AGE
traefik   LoadBalancer   10.43.201.14   10.20.1.87     80:31234/TCP,443:30987/TCP   25m
```

`EXTERNAL-IP` being the node's **private** address is correct and is what ServiceLB does â€” the
klipper-lb DaemonSet (`svclb-traefik-*` in `kube-system`) binds host ports 80 and 443 on the node
and forwards to Traefik. The node's public IP maps to that private address at the AWS layer.

Confirm the Ingress was accepted:

```bash
sudo kubectl -n citizen-portal get ingress
sudo kubectl -n citizen-portal describe ingress citizen-portal-ingress
```

Expected:

```
NAME                      CLASS     HOSTS   ADDRESS       PORTS   AGE
citizen-portal-ingress    traefik   *       10.20.1.87    80      4m
```

`CLASS` must be `traefik`, not `nginx`. `HOSTS` is `*` â€” the AWS overlay replaces the base's
`host: citizen-portal.local` rule with a host-less one, because this environment is reached by raw
IP and there is no DNS name, so a host-scoped rule would never match.

Now test the two routes from the node:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://localhost/
curl -sS http://localhost/api/services | head -c 300; echo
```

Expected:

```
200
[{"id":"...","name":"Passport Renewal","department":"Ministry of Interior",...
```

Then from **your own machine**, which is the test that actually matters:

```bash
curl -sS -o /dev/null -w 'frontend: %{http_code}\n' "http://${PUBLIC_IP}/"
curl -sS -o /dev/null -w 'api:      %{http_code}\n' "http://${PUBLIC_IP}/api/services"
```

Expected:

```
frontend: 200
api:      200
```

Open `http://${PUBLIC_IP}` in a browser. You should get the portal, and the service list should
populate â€” if the page renders but the list is empty, the frontend was built with the wrong
`VITE_API_BASE_URL` (step 16) or the CORS origin placeholder was not substituted.

> **Path precedence.** `/api` must win over `/`. Traefik sorts rules by path length descending, so
> the more specific prefix matches first regardless of the order in the manifest â€” the same
> behaviour ingress-nginx gives, so the base's ordering assumption still holds under Traefik.

`notification-service` is deliberately absent from the Ingress. There is no route to it from
outside the cluster, and that is the intended boundary, not a gap.

## Step 21. Verify PostgreSQL

Two separate databases, one per service, each its own Deployment and PVC. That boundary is
architectural, not incidental: neither service can read the other's tables.

```bash
sudo kubectl -n citizen-portal get pvc
```

Expected:

```
NAME                        STATUS   VOLUME     CAPACITY   ACCESS MODES   STORAGECLASS   AGE
citizen-postgres-pvc        Bound    pvc-a1b2   5Gi        RWO            local-path     5m
notification-postgres-pvc   Bound    pvc-c3d4   2Gi        RWO            local-path     5m
sentinel-data-pvc           Bound    pvc-e5f6   1Gi        RWO            local-path     5m
```

All `Bound`, all `local-path`, all `RWO`. `ReadWriteOnce` is fine here because there is exactly
one node â€” every pod that could mount a volume is on the same machine. On a multi-node cluster
this would be a real constraint.

Check the databases answer:

```bash
sudo kubectl -n citizen-portal exec deploy/citizen-postgres -- pg_isready -U app_user
sudo kubectl -n citizen-portal exec deploy/notification-postgres -- pg_isready -U app_user
```

Expected:

```
/var/run/postgresql:5432 - accepting connections
/var/run/postgresql:5432 - accepting connections
```

Confirm the migrations ran and the seed data landed:

```bash
sudo kubectl -n citizen-portal exec deploy/citizen-postgres -- \
  psql -U app_user -d citizen_portal -c '\dt'

sudo kubectl -n citizen-portal exec deploy/citizen-postgres -- \
  psql -U app_user -d citizen_portal -c 'SELECT count(*) FROM services;'
```

Expected:

```
              List of relations
 Schema |      Name       | Type  |  Owner
--------+-----------------+-------+----------
 public | alembic_version | table | app_user
 public | citizens        | table | app_user
 public | requests        | table | app_user
 public | services        | table | app_user

 count
-------
     6
(1 row)
```

A count of `0` means the `migrate-and-seed` init container did not complete. Check it:

```bash
sudo kubectl -n citizen-portal logs deploy/citizen-service -c migrate-and-seed
```

### Where this data actually lives, and what that means

K3s's `local-path` provisioner writes PersistentVolumes to `/var/lib/rancher/k3s/storage` on the
node's root EBS volume:

```bash
sudo ls -la /var/lib/rancher/k3s/storage/
```

Expected:

```
drwxr-xr-x 3 root root 4096 Aug 21 09:38 pvc-a1b2..._citizen-portal_citizen-postgres-pvc
drwxr-xr-x 3 root root 4096 Aug 21 09:38 pvc-c3d4..._citizen-portal_notification-postgres-pvc
drwxr-xr-x 3 root root 4096 Aug 21 09:38 pvc-e5f6..._citizen-portal_sentinel-data-pvc
```

Be precise about what this survives:

- **Pod recreation, restarts and rollouts: yes.** The data is on the host, not in the container.
- **An instance reboot: yes.** The EBS volume persists across a stop/start.
- **Instance replacement: no.** `delete_on_termination = true` on the root volume, so terminating
  the instance destroys every database, every metric, every log and Sentinel's entire incident
  history. That is deliberate â€” an orphaned volume left behind after the instance is gone is just
  a bill â€” but it means the two lifecycle guards in step 11 are the only thing standing between
  a careless `terraform destroy` and total data loss.

**This is not a backup strategy.** There are no snapshots, no point-in-time recovery, and no
tested restore. If the data matters, take EBS snapshots or `pg_dump` to somewhere else:

```bash
sudo kubectl -n citizen-portal exec deploy/citizen-postgres -- \
  pg_dump -U app_user citizen_portal | gzip > /tmp/citizen_portal-$(date +%F).sql.gz
```

---

# Part 7 â€” Verify the observability stack and Sentinel (steps 22â€“26)

## How to reach a ClusterIP-only component

Every component in this part â€” Prometheus, Loki, Grafana, Alertmanager, Sentinel â€” is a
**ClusterIP** Service. None of them has an Ingress rule, a NodePort, or a security group rule.
From outside the VPC they do not exist. That is the point: the only thing this instance serves to
the internet is the citizen portal on port 80.

So verification goes through `kubectl port-forward` inside an SSM session. There are two shapes,
and which you want depends on whether you need a browser.

**Shape A â€” curl from the node.** Simplest, and all the checks below use it. Inside an SSM
session, background a port-forward and curl `localhost`:

```bash
sudo KUBECONFIG=/etc/rancher/k3s/k3s.yaml \
  kubectl -n citizen-portal port-forward svc/prometheus 9090:9090 >/tmp/pf-prom.log 2>&1 &
sleep 2
curl -sS http://localhost:9090/-/healthy
```

Kill it when done with `sudo pkill -f 'port-forward svc/prometheus'`.

**Shape B â€” a browser on your own machine.** Needed for Grafana. Two hops: a `port-forward` on the
node, plus an SSM port-forwarding session from your laptop to that port on the node.

On the node, start the forward detached so it survives the session ending:

```bash
sudo nohup env KUBECONFIG=/etc/rancher/k3s/k3s.yaml \
  kubectl -n citizen-portal port-forward svc/grafana 3000:3000 \
  >/var/log/pf-grafana.log 2>&1 &
exit
```

On your own machine:

```bash
aws ssm start-session \
  --target "$INSTANCE_ID" --region "$AWS_REGION" \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["3000"],"localPortNumber":["3000"]}'
```

Expected:

```
Starting session with SessionId: you@example.com-0f1e2d3c4b5a
Port 3000 opened for sessionId you@example.com-0f1e2d3c4b5a.
Waiting for connections...
```

Then open `http://localhost:3000` in your browser. `AWS-StartPortForwardingSession` connects to
`localhost:<portNumber>` **on the instance**, which is exactly where `kubectl port-forward` is
listening. Nothing is exposed publicly at any point â€” the traffic goes over the SSM control
channel.

Remember to clean up the detached forwards afterwards: `sudo pkill -f 'kubectl.*port-forward'`.

## Step 22. Verify Prometheus

```bash
sudo KUBECONFIG=/etc/rancher/k3s/k3s.yaml \
  kubectl -n citizen-portal port-forward svc/prometheus 9090:9090 >/tmp/pf-prom.log 2>&1 &
sleep 3
```

Health and config:

```bash
curl -sS http://localhost:9090/-/healthy
curl -sS http://localhost:9090/-/ready
```

Expected:

```
Prometheus Server is Healthy.
Prometheus Server is Ready.
```

**Are the scrape targets up?** This is the check that matters â€” a healthy Prometheus scraping
nothing is worse than no Prometheus, because the dashboards will be empty and the alerts will
never fire.

```bash
curl -sS http://localhost:9090/api/v1/targets \
  | jq -r '.data.activeTargets[] | "\(.health)\t\(.labels.app // .labels.job)\t\(.scrapeUrl)"' \
  | sort
```

Expected:

```
up	alertmanager	http://10.42.0.14:9093/metrics
up	citizen-service	http://10.42.0.19:8000/metrics
up	frontend	http://10.42.0.21:3000/metrics
up	grafana	http://10.42.0.17:3000/metrics
up	loki	http://10.42.0.16:3100/metrics
up	notification-service	http://10.42.0.20:8000/metrics
up	prometheus	http://10.42.0.15:9090/metrics
up	sentinel-ai	http://10.42.0.22:8080/metrics
```

Every target `up`. Discovery is annotation-based (`prometheus.io/scrape`), so a pod missing from
this list is missing its annotation rather than broken.

Count them the blunt way, which is the query Sentinel itself leans on:

```bash
curl -sSG http://localhost:9090/api/v1/query \
  --data-urlencode 'query=up{kubernetes_namespace="citizen-portal"}' \
  | jq -r '.data.result[] | "\(.metric.app)\t\(.value[1])"' | sort
```

Expected â€” every value `1`:

```
alertmanager	1
citizen-service	1
frontend	1
grafana	1
loki	1
notification-service	1
prometheus	1
sentinel-ai	1
```

Confirm application metrics actually exist, not just that the target is reachable:

```bash
curl -sSG http://localhost:9090/api/v1/query \
  --data-urlencode 'query=sum(rate(http_requests_total{app="citizen-service"}[5m]))' \
  | jq '.data.result'
```

A non-empty result means the counter exists and is being scraped. An empty result immediately
after deploy is normal â€” nothing has made a request yet. Run `curl http://localhost/api/services`
a few times and query again.

Confirm the alert rules loaded:

```bash
curl -sS http://localhost:9090/api/v1/rules \
  | jq -r '.data.groups[].rules[] | select(.type=="alerting") | .name' | sort
```

Expected:

```
ChaosCPUBurn
ChaosDatabaseFailure
ChaosForcedHTTPFailures
ChaosLatencyInjection
ChaosMemoryLeak
HighCPUUsage
HighHTTPErrorRate
HighRequestLatency
MemoryLeakSuspected
NotificationDeliveryFailureRateHigh
NotificationDispatchFailures
SentinelDown
SentinelEscalating
ServiceDown
```

Fourteen rules â€” nine from the base, five added by the overlay's `patch-monitoring.yaml`
(including `SentinelDown` and `SentinelEscalating`, which watch the watcher).

Clean up: `sudo pkill -f 'port-forward svc/prometheus'`.

## Step 23. Verify Loki

```bash
sudo KUBECONFIG=/etc/rancher/k3s/k3s.yaml \
  kubectl -n citizen-portal port-forward svc/loki 3100:3100 >/tmp/pf-loki.log 2>&1 &
sleep 3
curl -sS http://localhost:3100/ready
```

Expected:

```
ready
```

`Ingester not ready: waiting for 15s after being ready` for the first fifteen seconds is normal.

**Are logs arriving?** Alloy runs as a DaemonSet, tails container logs, and relabels
`__meta_kubernetes_pod_label_app` to `app` â€” which is the label every Deployment in `k8s/` already
sets, so it is the label to query on.

```bash
curl -sSG http://localhost:3100/loki/api/v1/labels | jq -r '.data[]'
```

Expected:

```
app
container
detected_level
level
namespace
pod
request_id
service_name
```

Then the actual query:

```bash
curl -sSG http://localhost:3100/loki/api/v1/query_range \
  --data-urlencode 'query={app="citizen-service"}' \
  --data-urlencode 'limit=5' \
  --data-urlencode "start=$(date -u -d '15 minutes ago' +%s)000000000" \
  --data-urlencode "end=$(date -u +%s)000000000" \
  | jq -r '.data.result[].values[][1]' | head -5
```

Expected â€” five JSON log lines from the service:

```
{"timestamp": "2026-08-21T09:44:12Z", "level": "info", "message": "GET /api/services 200", "request_id": "..."}
{"timestamp": "2026-08-21T09:44:09Z", "level": "info", "message": "Application startup complete", ...}
...
```

An empty `result` array with a `200` response means Loki is healthy but Alloy shipped nothing.
Check Alloy:

```bash
sudo kubectl -n citizen-portal logs daemonset/alloy --tail=30
```

Confirm the parsed labels work â€” Alloy parses the JSON logs so `level` becomes a queryable label:

```bash
curl -sSG http://localhost:3100/loki/api/v1/query_range \
  --data-urlencode 'query={app="citizen-service", level="error"}' \
  --data-urlencode 'limit=10' \
  --data-urlencode "start=$(date -u -d '1 hour ago' +%s)000000000" \
  --data-urlencode "end=$(date -u +%s)000000000" \
  | jq '.data.result | length'
```

Expected on a healthy system: `0`. This is the query that becomes interesting during step 28.

> **A real limitation.** Loki here writes to local filesystem storage on the node's root volume,
> with the same durability as everything else in step 21: fine across pod restarts, gone with the
> instance. There is no object-storage backend and no retention tuning. For a demo whose incidents
> last minutes, that is adequate; for anything with a compliance retention requirement it is not.

Clean up: `sudo pkill -f 'port-forward svc/loki'`.

## Step 24. Verify Grafana

```bash
sudo KUBECONFIG=/etc/rancher/k3s/k3s.yaml \
  kubectl -n citizen-portal port-forward svc/grafana 3000:3000 >/tmp/pf-graf.log 2>&1 &
sleep 3
curl -sS http://localhost:3000/api/health | jq
```

Expected:

```json
{
  "commit": "...",
  "database": "ok",
  "version": "11.2.0"
}
```

`"database": "ok"` is the one to read. Grafana's own SQLite is fine.

Confirm both datasources were provisioned. This needs the admin password from step 18a:

```bash
GRAFANA_PW='<the password printed by generate-aws-secrets.sh>'

curl -sS -u "admin:${GRAFANA_PW}" http://localhost:3000/api/datasources \
  | jq -r '.[] | "\(.name)\t\(.type)\t\(.url)"'
```

Expected:

```
Prometheus	prometheus	http://prometheus:9090
Loki	        loki	    http://loki:3100
```

Test that Grafana can actually reach them, rather than merely having them configured:

```bash
curl -sS -u "admin:${GRAFANA_PW}" \
  http://localhost:3000/api/datasources/name/Prometheus \
  | jq -r '.id' \
  | xargs -I{} curl -sS -u "admin:${GRAFANA_PW}" \
      "http://localhost:3000/api/datasources/{}/health" | jq
```

Expected:

```json
{
  "status": "OK",
  "message": "Successfully queried the Prometheus API."
}
```

List the provisioned dashboards:

```bash
curl -sS -u "admin:${GRAFANA_PW}" 'http://localhost:3000/api/search?type=dash-db' \
  | jq -r '.[] | "\(.title)\t\(.uid)"'
```

For the browser, use Shape B above and log in as `admin` with that password. If you would rather
not paste the password around, note that it is in the cluster:

```bash
sudo kubectl -n citizen-portal get secret grafana-secret \
  -o jsonpath='{.data.GF_SECURITY_ADMIN_PASSWORD}' | base64 -d; echo
```

Clean up: `sudo pkill -f 'port-forward svc/grafana'`.

## Step 25. Verify Alertmanager

```bash
sudo KUBECONFIG=/etc/rancher/k3s/k3s.yaml \
  kubectl -n citizen-portal port-forward svc/alertmanager 9093:9093 >/tmp/pf-am.log 2>&1 &
sleep 3
curl -sS http://localhost:9093/-/healthy
curl -sS http://localhost:9093/api/v2/status | jq '.cluster.status, .versionInfo.version'
```

Expected:

```
OK
"ready"
"0.27.0"
```

List current alerts:

```bash
curl -sS http://localhost:9093/api/v2/alerts | jq -r '.[] | "\(.labels.alertname)\t\(.status.state)"'
```

Expected on a healthy system: **empty output**. No alerts firing is the correct steady state, and
it is also the baseline you need before step 28 â€” an environment that is already alerting makes it
impossible to tell which alert your chaos scenario caused.

**The critical check is the receiver.** This is the wire between the monitoring stack and the
autonomous agent; if it is wrong, everything downstream in steps 29â€“31 silently does nothing.

```bash
curl -sS http://localhost:9093/api/v2/status \
  | jq -r '.config.original' | grep -A4 'name: sentinel'
```

Expected:

```yaml
      - name: sentinel
        webhook_configs:
          - url: http://sentinel-ai:8080/api/alerts/webhook
            send_resolved: true
```

That URL must be exactly `http://sentinel-ai:8080/api/alerts/webhook`. It is an in-cluster
ClusterIP address, so Alertmanager reaches Sentinel over the pod network â€” nothing leaves the
node.

You can exercise the path end to end without waiting for a real incident, by posting a synthetic
alert into Alertmanager and watching it arrive at Sentinel:

```bash
curl -sS -X POST http://localhost:9093/api/v2/alerts \
  -H 'Content-Type: application/json' \
  -d '[{
        "labels": {
          "alertname": "ManualWebhookProbe",
          "severity": "warning",
          "app": "citizen-service",
          "namespace": "citizen-portal"
        },
        "annotations": { "summary": "synthetic alert, verifying the Sentinel webhook path" },
        "startsAt": "'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'"
      }]'
```

Expected: an empty `200` response. Then confirm it arrived:

```bash
sudo kubectl -n citizen-portal logs deploy/sentinel-ai --tail=20 | grep -i webhook
```

Expected â€” a log line showing Sentinel received `ManualWebhookProbe`. Sentinel's policy engine
will decline to act on an alert it has no rule for, which is the correct outcome: the point of
this probe is the transport, not the remediation.

Clean up: `sudo pkill -f 'port-forward svc/alertmanager'`.

## Step 26. Verify Sentinel

```bash
sudo KUBECONFIG=/etc/rancher/k3s/k3s.yaml \
  kubectl -n citizen-portal port-forward svc/sentinel-ai 8080:8080 >/tmp/pf-sent.log 2>&1 &
sleep 3
curl -sS http://localhost:8080/healthz
curl -sS http://localhost:8080/readyz | jq
```

Expected:

```
{"status":"ok"}
```

and from `/readyz`:

```json
{
  "status": "ready",
  "prometheus": "ok",
  "loki": "ok",
  "kubernetes": "ok",
  "openai": "not configured",
  "github": "not configured",
  "slack": "not configured"
}
```

The three `not configured` entries are expected unless you filled in `sentinel.env`, and they are
**not** a failure: `/readyz` returns a degraded-but-200 for unconfigured optional integrations,
and non-200 only when Prometheus or the Kubernetes API are unreachable.

> **Why liveness and readiness differ here.** `/healthz` is process-liveness only and never
> touches Prometheus, Loki or the Kubernetes API. That separation matters more than usual: if
> Sentinel's liveness probe depended on Prometheus being reachable, a Prometheus outage would
> restart Sentinel in a loop â€” taking out the thing that is supposed to be diagnosing the outage.

The incidents API, which is the interface you will actually use in steps 29â€“31:

```bash
curl -sS http://localhost:8080/api/incidents | jq
```

Expected on a fresh deploy:

```json
{ "incidents": [], "total": 0 }
```

Confirm the effective configuration â€” specifically that autonomy is on and the allow-lists are
what you expect:

```bash
sudo kubectl -n citizen-portal get configmap sentinel-config -o jsonpath='{.data}' | jq
```

Expected, abbreviated:

```json
{
  "DRY_RUN": "false",
  "ALLOWED_NAMESPACES": "citizen-portal",
  "ALLOWED_DEPLOYMENTS": "citizen-service,notification-service,frontend",
  "CONFIDENCE_ROLLBACK": "0.95",
  "CONFIDENCE_RESTART": "0.90",
  "CONFIDENCE_SCALE": "0.90",
  "CONFIDENCE_RESET_CHAOS": "0.90",
  "MIN_REPLICAS": "1",
  "MAX_REPLICAS": "3",
  "MAX_ACTIONS_PER_INCIDENT": "3",
  "ACTION_COOLDOWN_SECONDS": "120",
  "DEPLOYMENT_CORRELATION_WINDOW_MINUTES": "30",
  "VALIDATION_SETTLE_SECONDS": "45",
  "VALIDATION_TIMEOUT_SECONDS": "300",
  "PROMETHEUS_URL": "http://prometheus:9090",
  "LOKI_URL": "http://loki:3100",
  "ALERTMANAGER_URL": "http://alertmanager:9093"
}
```

Read four things off that:

- **`DRY_RUN: "false"`** means Sentinel actually executes remediation, including rollback, with no
  human approval step anywhere. That is the intended behaviour and the whole point. Set it to
  `"true"` to have Sentinel run the full lifecycle and log the action it *would* have taken â€”
  which is the honest way to demo it to someone not yet comfortable with it acting unattended.
- **Four autonomous actions** exist and no others: `restart_deployment`, `rollback_deployment`,
  `scale_deployment`, `reset_chaos_fault`.
- **`ALLOWED_DEPLOYMENTS` excludes both databases.** `citizen-postgres` and
  `notification-postgres` are deliberately non-remediable. Restarting a PostgreSQL pod under load
  is a plausible-looking action that risks data loss and fixes almost nothing, and rolling one back
  is meaningless. A database incident escalates to a human instead. That is the correct answer, not
  a limitation.
- **Rollback carries the highest confidence bar (0.95)** because it is the action with the largest
  effect: it changes what code is running. These are policy thresholds, not guarantees of
  correctness â€” a confidence number produced partly by an LLM is a heuristic. The preconditions
  checked alongside it (does a previous revision exist, does a deploy correlate with the incident
  onset) are the parts that actually make rollback safe.

Confirm the RBAC bound to Sentinel is genuinely narrow. This is the last line of defence, and the
only one that holds even if the policy engine has a bug:

```bash
for verb in delete create; do
  for res in pods secrets deployments; do
    printf '%s %s: ' "$verb" "$res"
    sudo kubectl auth can-i "$verb" "$res" \
      --as=system:serviceaccount:citizen-portal:sentinel-ai -n citizen-portal
  done
done

printf 'exec into pods: '
sudo kubectl auth can-i create pods/exec \
  --as=system:serviceaccount:citizen-portal:sentinel-ai -n citizen-portal

printf 'read secrets: '
sudo kubectl auth can-i get secrets \
  --as=system:serviceaccount:citizen-portal:sentinel-ai -n citizen-portal

printf 'patch deployments: '
sudo kubectl auth can-i patch deployments \
  --as=system:serviceaccount:citizen-portal:sentinel-ai -n citizen-portal

printf 'read kube-system pods: '
sudo kubectl auth can-i get pods \
  --as=system:serviceaccount:citizen-portal:sentinel-ai -n kube-system
```

Expected:

```
delete pods: no
delete secrets: no
delete deployments: no
create pods: no
create secrets: no
create deployments: no
exec into pods: no
read secrets: no
patch deployments: yes
read kube-system pods: no
```

`patch deployments: yes` is the only write Sentinel has, and it is what all three Kubernetes
actions are built from â€” restart patches the pod template's annotations, rollback patches the pod
template back to a previous ReplicaSet's, scale patches replicas. Everything else is `no`.

The `no` on `pods/exec` is the most important line in that output: it makes "the LLM never gets
shell access" a structural fact enforced by the API server, rather than a coding convention that a
bug could violate. The `no` on secrets means a confused or compromised agent cannot read
application credentials, and therefore cannot include them in an incident report, a GitHub issue,
or a prompt sent to an LLM â€” that last one being the real risk.

Sentinel also has **no AWS permissions of any kind.** It runs as a pod with no IAM role, so it
cannot touch ECR, EC2, or any AWS resource. The instance profile belongs to the node, not to the
pod, and nothing mounts credentials into Sentinel's container.

Clean up: `sudo pkill -f 'port-forward svc/sentinel-ai'`.
