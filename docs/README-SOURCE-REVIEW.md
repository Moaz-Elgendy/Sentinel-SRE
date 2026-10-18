# README source review

## Review boundary

The README was prepared from the uploaded `Sentinel-SRE-main.zip`, five presentation JPGs, one GUI PNG, and one MP4. The ZIP did not contain a root `README.md`; this package supplies one. Application source was read without modification. No live deployment, application test suite, container build, Terraform plan/apply, or CI run was executed during this documentation task.

Verification consisted of tracing claims and commands to source, checking local Markdown targets against the package overlaid on the uploaded repository, inspecting the supplied images and sampled video frames, and checking the MP4 duration. Mermaid is provided as conservative GitHub-compatible flowchart syntax; no GitHub rendering or interactive behavior was tested.

## Claim-to-source map

| README subject | Primary source inspected |
|---|---|
| Compose services, ports, migration/seed startup | `docker-compose.yml`, `.env.example` |
| Local deployment scope | `scripts/deploy-kind.sh`, `k8s/kustomization.yaml`, `k8s/overlays/local/` |
| External control plane | `k8s/overlays/aws/kustomization.yaml`, `infra/terraform/sentinel_remote.tf`, `sentinel_user_data.sh.tftpl` |
| Region, instance sizes, remote enablement | `infra/terraform/variables.tf` (actual defaults, not historical comments) |
| Security-group boundaries | `infra/terraform/security.tf`, `infra/terraform/sentinel_remote.tf` |
| CI gates, triggers, image tags, manual modes, deployment | `.github/workflows/ci-cd.yml`, actual jobs and shell bodies |
| Sentinel startup and routes | `sentinel-ai/app/main.py`, `app/routers/health.py`, `app/routers/alerts.py` |
| Default gates, limits, auth, connector settings | `sentinel-ai/app/core/config.py` |
| Autonomous/human authorization policy | `sentinel-ai/app/lifecycle/policy.py`, `remediation.py` |
| Deep investigation and typed fix validation | `sentinel-ai/app/lifecycle/deep_investigation.py`, `deep_investigation_tools.py`, `app/models/incident.py` |
| Recovery checks and metric-window caveat | `sentinel-ai/app/lifecycle/validation.py` |
| Incident identity/concurrency and bounds | `sentinel-ai/app/lifecycle/incident_manager.py`, `orchestrator.py`, `docs/incident-engine.md` |
| Learning and recorded-evidence replay | `sentinel-ai/app/lifecycle/learning.py`, `replay.py` |
| Single active environment; AWS evidence gap | `sentinel-ai/app/routers/environments.py`, `alerts.py`, `app/lifecycle/investigation.py` |
| GUI development configuration | `sentinel-gui/package.json`, `vite.config.js`, `.env.example`, `src/api/client.js` |
| Functional and fault-test commands | `scripts/smoke-test.sh`, `scripts/incident-scenarios.sh` |

## Conflicts handled explicitly

- `docs/aws-deployment.md` and `k8s/README.md` contain old statements that no AWS deployment occurred. These conflict with the owner's later deployment report and supplied runtime demonstration. The new README attributes the deployment history and does not repeat the old status as a current fact.
- Several Terraform comments still describe remote Sentinel as disabled by default. The variable value is currently `true`; its current default is documented.
- Older runbooks mention `eu-west-1` and `t3a.large`. Current source defaults are `eu-central-1` and `m7i-flex.large` for the workload, with `t3.small` for Sentinel.
- Old comments describe an in-cluster Sentinel Deployment. The active AWS overlay omits that deployment; external EC2 bootstrap manages the agent and GUI.
- A CI slide says code-only deploys use image updates without manifest application. Current K3s workflow always calls `apply-manifests`, with conditional manifest synchronization.
- External Sentinel delivery pulls `latest` and checks systemd activity. It does not pin the deployed image to `GITHUB_SHA` or probe API readiness. Its ECR prefix is hardcoded, unlike the configurable build prefix.
- Multi-environment identifiers, registration, and storage do not establish simultaneous multi-environment operation. Environment registration replaces the current context.
- The optional EC2/CloudWatch connector exists but is not used by the incident evidence collector. AWS-hosted deployment is distinct from AWS-native incident evidence integration.
- Historical report test counts are not current verification results and were omitted. No uptime, MTTR improvement, accuracy, coverage, or production-readiness claim was inferred from screenshots.

## Suggested follow-up maintenance

Reconcile the historical runbooks and comments with the current two-node topology; make external Sentinel delivery SHA-pinned and readiness-checked; align the ECR prefix in all jobs; confirm Node/package engine compatibility in a fresh build; record dated CI and deployment evidence. These are follow-up implementation tasks, not changes made by this README package.
