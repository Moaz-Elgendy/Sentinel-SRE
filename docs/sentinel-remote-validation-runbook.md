# Validating Sentinel against a real remote K3s cluster

**This has not been run.** Everything in this document is infrastructure and
instructions for YOU to execute — I cannot provision AWS resources or reach a
real K3s cluster from the sandboxed environment this was written in (no
`docker`/`kind`/`k3s` binary available, and network egress is allowlisted to
package registries only, not `amazonaws.com` or any customer host — a live
check against `ec2.amazonaws.com` from that sandbox returned an egress-proxy
403, not an AWS response). What follows is the concrete, minimum-diff path to
run the acceptance test yourself and get a real answer. Do not treat any
part of `docs/sentinel-integration.md` as validated against a real cluster
until you have actually run this.

## What this buys you

A second EC2 instance (`infra/terraform/sentinel_remote.tf`, gated behind
`enable_remote_sentinel`, off by default) running Sentinel as a plain Docker
container — not inside K3s — authenticating to the K3s node's Kubernetes API
over the private VPC network, reaching Prometheus/Loki through two new
NodePort Services, with the K3s node's Alertmanager configured to webhook
back to it. This is the target topology from the spec, made real with the
smallest infrastructure diff on top of the existing single-instance stack.

The existing in-cluster Sentinel Deployment (`k8s/overlays/aws/sentinel/`)
is untouched and keeps working independently — this is additive, not a
replacement, per the explicit instruction not to do another large refactor.

## Cost note

A second `t3.micro` is roughly the same order of cost as the existing K3s
node. If that is not acceptable for this demo cycle, everything through
step 4 (routing, Alertmanager config, environment registration) still works
identically if you run Sentinel as a plain Docker container on your own
laptop/machine *inside the same network path* instead of a second EC2 — see
"Cheaper alternative: run Sentinel locally" at the end. The webhook step
(Alertmanager -> Sentinel) needs Sentinel to be reachable from the VPC,
which a laptop behind home NAT generally is not without a tunnel — that is
the one thing the two-EC2 topology gets you that a laptop does not.

## Prerequisites

- The existing single-instance stack already applied and the application
  already deployed and healthy (`docs/aws-deployment.md` steps 1-26). Do
  not attempt this before that baseline works — if the K3s cluster itself
  isn't healthy, nothing below will tell you anything useful.
- AWS CLI configured with credentials for this account.
- `kubectl` access to the K3s cluster (via SSM session onto the K3s node, as
  already documented).

## Step 1 — Deploy the observability NodePort Services

```bash
aws ssm start-session --target <k3s-instance-id> --region <region>
cd /opt/sentinel-sre   # or wherever the repo checkout lives on the node
sudo kubectl apply -k k8s/overlays/aws/
sudo kubectl -n citizen-portal get svc prometheus-nodeport loki-nodeport
```

Confirm both show `NodePort` type with `9090:30090/TCP` and `3100:30100/TCP`.
This does not change the existing ClusterIP Services or anything else in the
overlay — it only adds two new Services.

## Step 2 — Generate the Kubernetes credential for external Sentinel

Still on the K3s node (via the SSM session):

```bash
# A long-lived token, explicitly a temporary prototype credential — see
# infra/terraform/sentinel_remote.tf's docstring for the production
# rotation note. 1 year is deliberately generous for a prototype; shorten
# it and re-run this + step 3's put-parameter on whatever cadence you want.
sudo kubectl create token sentinel-ai -n citizen-portal --duration=8760h > /tmp/sentinel-token.txt
cat /tmp/sentinel-token.txt

# CA cert, base64-encoded, so Sentinel can verify TLS instead of falling
# back to verify_ssl=false.
sudo cat /etc/rancher/k3s/k3s.yaml | grep 'certificate-authority-data' | awk '{print $2}' > /tmp/sentinel-ca.b64
cat /tmp/sentinel-ca.b64
```

Copy both values out of the session (they're short-lived on your clipboard,
not written anywhere persistent by this step).

## Step 3 — Store the credential in SSM Parameter Store

From your own machine, NOT the K3s node (these need your AWS credentials,
not the node's instance role — the node's role deliberately cannot write to
Parameter Store, only Sentinel's role can *read* its own two parameters):

```bash
aws ssm put-parameter --region <region> \
  --name /sentinel-sre-demo/sentinel/k8s-token \
  --type SecureString --overwrite \
  --value "$(cat sentinel-token.txt)"

aws ssm put-parameter --region <region> \
  --name /sentinel-sre-demo/sentinel/k8s-ca-cert-b64 \
  --type SecureString --overwrite \
  --value "$(cat sentinel-ca.b64)"

# Optional: OpenAI/Gemini/GitHub/Slack/chaos-admin, if you want the LLM
# and integrations enabled for this test. Rule-based-only is a fully
# supported mode if you skip this.
aws ssm put-parameter --region <region> \
  --name /sentinel-sre-demo/sentinel/extra-env \
  --type SecureString --overwrite \
  --value "$(printf 'OPENAI_API_KEY=sk-...\nGITHUB_TOKEN=ghp_...\nGITHUB_REPOSITORY=you/your-fork\n')"
```

Delete the local token/CA files after this — they were only ever meant to
transit your terminal.

## Step 4 — Push a Sentinel image tag Terraform can pull

`infra/terraform/sentinel_remote.tf` pulls `sentinel-ai:${var.sentinel_image_tag}`
(default `"latest"`) from the same ECR repository CI already pushes to. If
CI does not already push a `latest` tag, either push one manually:

```bash
docker tag <account>.dkr.ecr.<region>.amazonaws.com/sentinel-sre-demo/sentinel-ai:<sha> \
           <account>.dkr.ecr.<region>.amazonaws.com/sentinel-sre-demo/sentinel-ai:latest
docker push <account>.dkr.ecr.<region>.amazonaws.com/sentinel-sre-demo/sentinel-ai:latest
```

or set `-var sentinel_image_tag=<sha>` on the apply in the next step.

## Step 5 — Apply the external-control-plane infrastructure

```bash
cd infra/terraform
terraform apply -var enable_remote_sentinel=true
```

This creates: the Sentinel security group and instance, the IAM
role/instance profile, the three SSM parameters (if step 3 already created
them, Terraform will show them as already existing / no-op on `value` due
to `ignore_changes`), and the three new ingress rules on the K3s node's
security group (6443, 30090, 30100 — each sourced only from the new
Sentinel security group).

```bash
terraform output sentinel_instance_id
terraform output sentinel_private_ip
terraform output sentinel_webhook_url
```

## Step 6 — Confirm Sentinel booted and can reach everything

```bash
aws ssm start-session --target $(terraform output -raw sentinel_instance_id) --region <region>
sudo cat /var/lib/sentinel/remote-bootstrap-complete
sudo systemctl status sentinel-ai
sudo journalctl -u sentinel-ai -n 100 --no-pager
curl -s http://localhost:8080/readyz | jq .
curl -s http://localhost:8080/environments | jq .
```

The bootstrapped environment's id is `demo-env-remote` (see
`sentinel_user_data.sh.tftpl`). Then, from the same session:

```bash
curl -s -X POST http://localhost:8080/environments/demo-env-remote/test-connection | jq .
```

**This is the first real checkpoint (acceptance-test items 1-6).** Every
connector in that response should show `"ok": true` for `kubernetes` and
`prometheus`/`loki`. If `kubernetes.ok` is `false`, check
`kubernetes.detail` — the most likely causes are the token not actually
being populated (step 2/3 skipped or mistyped the parameter name) or the
security-group rule not being live yet (re-check `terraform apply` finished
without error). If `prometheus.ok`/`loki.ok` are false, check that step 1's
`kubectl apply -k` actually landed and that the security group rule for
30090/30100 exists (`aws ec2 describe-security-groups` on the K3s node's
SG).

## Step 7 — Point the remote Alertmanager at external Sentinel

On the K3s node, edit (or patch, if using the existing
`patch-monitoring.yaml` kustomize patch) Alertmanager's config to add/point
its webhook receiver at:

```
$(terraform output -raw sentinel_webhook_url)
```

i.e. `http://<sentinel-private-ip>:8080/api/alerts/webhook`. Apply and
restart Alertmanager, then confirm it can reach that URL — Alertmanager's
own `/api/v2/status` page shows recent notification attempts, or check
Sentinel's logs for an inbound request when you trigger a test alert.

## Step 8 — Run the primary acceptance test: bad-deployment

This is `scripts/incident-scenarios.sh`'s existing `bad-deployment`
scenario (see `docs/sentinel-integration.md`'s "Testing the loop" section) —
unchanged, just now pointed at external Sentinel instead of in-cluster:

```bash
# on the K3s node
./scripts/incident-scenarios.sh bad-deployment
```

Then, watching `sudo journalctl -u sentinel-ai -f` on the Sentinel
instance, confirm in order (this is acceptance-test items 6-14):

1. An incident is created (`incident_created` or similar log line) shortly
   after Alertmanager's alert fires.
2. Evidence collection log lines show Prometheus/Loki/Kubernetes/GitHub
   calls succeeding, not erroring.
3. RCA produces a `bad_deployment` hypothesis.
4. Decision/Policy select `rollback_deployment`.
5. The rollback actually happens — `kubectl -n citizen-portal rollout
   history deployment/citizen-service` on the K3s node shows a new
   revision.
6. Validation passes and the incident is marked `RESOLVED` —
   `curl http://localhost:8080/incidents/<id>` from the Sentinel instance.
7. `curl http://localhost:8080/incidents` shows the incident persisted with
   `environment_id: "demo-env-remote"`.

## What "done" looks like

Fill this in after actually running the above, and treat
`docs/sentinel-integration.md` as updated only once you have:

```
[ ] Step 6 test-connection: kubernetes.ok = ?      loki.ok = ?
                             prometheus.ok = ?      github.ok = ?
[ ] Step 7: Alertmanager successfully reached Sentinel's webhook?  Y/N
[ ] Step 8.1 incident created?             Y/N
[ ] Step 8.2 evidence collection clean?    Y/N   (paste any connector errors)
[ ] Step 8.3 RCA = bad_deployment?         Y/N   (confidence: ___)
[ ] Step 8.4 action = rollback_deployment? Y/N
[ ] Step 8.5 rollback actually applied?    Y/N
[ ] Step 8.6 incident RESOLVED?            Y/N   (recovery_time_seconds: ___)
[ ] Step 8.7 incident persisted correctly? Y/N

Blockers hit: ___
Time from alert firing to RESOLVED: ___
```

## Cheaper alternative: run Sentinel locally (read-only validation only)

If a second EC2 instance genuinely isn't affordable right now, you can
validate items 1-4 of the acceptance test (Sentinel authenticates, reads
pods/deployments/events, reaches Prometheus/Loki) without any new AWS spend,
using SSM port-forwarding instead of a second instance:

```bash
# Forward the K3s API (already listening on the node's own interface)
aws ssm start-session --target <k3s-instance-id> --region <region> \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["6443"],"localPortNumber":["6443"]}'

# In another terminal: same pattern for the two NodePorts, after step 1 above
aws ssm start-session --target <k3s-instance-id> --region <region> \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["30090"],"localPortNumber":["9090"]}'
```

Then run Sentinel locally with `KUBERNETES_API_SERVER=https://localhost:6443`,
`PROMETHEUS_URL=http://localhost:9090`, etc. This does NOT satisfy the full
acceptance test — a laptop behind NAT cannot receive Alertmanager's inbound
webhook without a public tunnel (ngrok or similar), so items 5, 7, and the
full trigger-to-resolution loop still need either the two-EC2 topology above
or a tunnel you set up and are comfortable with the security implications of.
Treat this path as "prove the connectors work," not as a substitute for
Step 8.
