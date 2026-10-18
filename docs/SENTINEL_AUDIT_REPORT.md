# Sentinel SRE — Engineering Audit & Stabilization Report

Repository: `Sentinel-SRE-main` (citizen-service, notification-service, frontend, sentinel-ai, sentinel-gui)
Audit date: 2026-09-21

---

## 1. Project understanding

The repo is a demo "digital citizen services portal" (citizen-service + notification-service + frontend, all FastAPI/React, Postgres-backed) with a self-contained autonomous SRE agent, **Sentinel**, bolted on top.

Sentinel's job: watch Prometheus/Loki/Kubernetes/Alertmanager, decide what's wrong, and — within a hard-coded, deny-by-default safety envelope — fix it without a human, or escalate. Its lifecycle is a fixed pipeline:

`DETECTION → INVESTIGATION → CORRELATION → ROOT_CAUSE_ANALYSIS → REMEDIATION_DECISION → POLICY_CHECK → AUTONOMOUS_EXECUTION → RECOVERY_VALIDATION → (re-investigation loop) → DOCUMENTATION → NOTIFICATION → LEARNING → ESCALATION`

The safety-critical boundary the codebase itself documents is `LLM → Decision Engine → Policy Engine → Remediation Engine → K8s`. Concretely:

- **Evidence** (`investigation.py`) is collected deterministically from Prometheus/Loki/Kubernetes/events — the LLM never chooses what evidence to gather.
- **RCA** (`rca.py`) runs a rules engine first; an LLM (Groq `openai/gpt-oss-120b`, via `Reasoner`/`ReasonerHealth`/`factory.py`) may only *narrow or annotate* that conclusion — `apply_llm_response()` discards anything where the LLM's proposed root cause/action doesn't match the rule engine's own conclusion, and clamps confidence. A missing API key or an open circuit breaker both fall back to pure rules, so nothing depends on the LLM being present.
- **Decision** (`decision.py`) turns a root cause into a small, typed `ActionPlan` from a fixed `ACTION_LADDER` — never free text.
- **Policy** (`policy.py`) is deny-by-default: frozen deny-lists (Postgres deployments, `kube-system` et al.) checked before any allow-list, confidence thresholds, action caps, cooldowns, and per-action preconditions (rollback has 7, including "recovery validation must be possible" and "a safe target revision must exist").
- **Remediation** (`remediation.py`) re-validates the allow-list independently (defense in depth) and only ever issues one of 4 typed actions with typed params — never a shell command or a raw K8s verb.
- **Kubernetes client** (`kubernetes_client.py`) is the only module that talks to the cluster: no shell, no `kubectl`, 3 patch verbs total (restart/rollback/scale), everything else read-only.
- **Recovery validation** (`validation.py`) is what actually closes an incident — it polls Deployment/pod state, a JSON-parsed health endpoint (not just the HTTP status code, since `/readyz` can return 200-with-degraded), metrics, and chaos gauges, and treats "I can't see it" as failure, never success.
- **Incident correlation/concurrency** (`incident_manager.py`) uses a single in-memory "lease" per incident id plus a `threading.RLock` around the whole decide-and-mutate step, so concurrent webhook deliveries can't race, duplicate alerts are folded into counters instead of re-triggering remediation, and a process restart mid-incident escalates rather than silently resuming.

Deployment is Kustomize-based: `k8s/base/` (shared manifests) + `k8s/overlays/local` (kind/minikube, committed demo secrets) + `k8s/overlays/aws` (Traefik, ECR images pinned by git SHA, secrets generated from gitignored `.env` files, Sentinel added as an external control plane on its own EC2 instance). CI/CD is GitHub Actions with OIDC-based AWS role assumption (no static keys) and SSM-mediated deploys (the Kubernetes API is never exposed to the internet).

---

## 2. Bugs fixed

### 2.1 `ImportError: cannot import name 'activity'` — sentinel-ai fails to start
- **Root cause:** `app/main.py` imports and mounts `app.routers.activity`, but that module didn't exist in the repo. Any import of `app.main` (including the entire pytest run) crashed immediately at collection.
- **Fix:** Implemented `app/routers/activity.py` — a read-only `GET /api/activity/status` endpoint (admin-JWT gated) that reports Sentinel's live state: which of Prometheus/Loki/Kubernetes/Alertmanager are reachable, the active incident's phase/timeline if one is running, and reasoner health. Built directly from the pre-existing test file `tests/test_activity_and_logs_api.py`, which already specified the exact contract, and modeled on the existing `dashboard.py` router's conventions.
- **Test:** `tests/test_activity_and_logs_api.py` (pre-existing, now passing) plus `tests/test_incident_engine.py::test_webhook_repeat_is_correlated_and_activity_lists_incidents`, which asserts the `reasoner`/`active_incidents` fields this endpoint must return.

### 2.2 `AttributeError: 'Evidence' object has no attribute 'init_container_logs'`
- **Root cause:** `orchestrator.py`'s `_investigate()` reads `evidence.init_container_logs`, but the `Evidence` dataclass never defined that field and nothing populated it — a collector that was referenced but never implemented. Every incident that reached investigation crashed here, which surfaced as 12 failures in `test_incident_engine.py`.
- **Fix:** Added `init_container_logs: list[dict] = field(default_factory=list)` to `Evidence` (with `to_dict`/`from_dict` support), and implemented the actual collector in `investigation.py`: when a pod has a failing init container (using the existing `CRASHLOOP_WAITING_REASONS` set from `correlation.py`), fetch its logs via `k8s.get_container_logs()`, gated on `k8s.available`, using the same fail-soft `asyncio.gather(..., return_exceptions=True)` pattern every other collector in that file uses.
- **Test:** the 12 previously-failing `test_incident_engine.py` cases, now passing.

### 2.3 `activity_status()` missing fields the test contract required
- **Root cause:** after fixing 2.1, my first implementation of `activity.py` didn't include `reasoner`/`active_incidents` in its response, which `test_webhook_repeat_is_correlated_and_activity_lists_incidents` asserts.
- **Fix:** added `_reasoner_status()` (surfacing `ReasonerHealth.snapshot()`) and an `active_incidents` count to both response branches (investigating and idle).
- **Test:** the same test, now passing.

**Net result of 2.1–2.3:** the full `sentinel-ai` suite went from `12 failed, 351 passed, 107 errors` (collection failures cascading from 2.1) to **474 passed, 0 failed**.

### 2.4 Rollback target selection could silently pick a broken ReplicaSet
- **Root cause:** this is the one the brief specifically asked me to re-verify, and it was **not** already fixed. `kubernetes_client.py`'s `find_previous_revision()` has two branches: with an explicit `target_revision` (human override), it correctly returns exactly that revision, whatever its validity — that's an intentional, explicit choice, and `patch_deployment_template()`'s `_assert_valid_container_images()` still refuses to apply it if the images are bad. But **without** an explicit target revision — the path the autonomous decision engine and a human-override-with-no-revision both take — it simply returned `numbered[1]`, the numerically-previous ReplicaSet, with **no check of its `images_valid` flag at all**. `correlation.py` already has the correct logic for this exact decision (`_find_valid_rollback_candidate()`, which skips invalid candidates) — it just was never applied inside `kubernetes_client.py` itself, so any code path that called `find_previous_revision()` directly (rather than going through `correlation.py`'s pre-computed `previous_revision`) could select the same kind of placeholder-image ReplicaSet that caused the real citizen-service incident. A grep confirmed **zero tests exercised this function at all** before this audit.
- **Fix:** rewrote the no-explicit-target branch to skip any candidate with `images_valid is False` (a missing key is treated as valid, so this can only make Sentinel skip a target it previously would have blindly used — never accept one it used to reject), mirroring `correlation.py`'s logic exactly. Left the explicit-target-revision branch untouched, since honoring an explicit choice (backstopped by the unconditional final gate in `patch_deployment_template`) is correct behavior, not a bug.
- **Test:** added 4 new regression tests to `tests/test_kubernetes_client.py`: skipping one invalid candidate to find a valid older one; returning `None` when every older revision is invalid; treating a missing `images_valid` key as valid (no false rejections); and confirming an explicit `target_revision` is unaffected by validity. All pass.

### 2.5 Dead/misleading legacy Kubernetes manifests
- **Root cause:** the repo contains two parallel K8s manifest trees: the live one (`k8s/base/` + `k8s/overlays/{local,aws}`, wired up by every `kustomization.yaml`) and a flat legacy one at `k8s/citizen-service/`, `k8s/frontend/`, `k8s/ingress/`, `k8s/monitoring/`, `k8s/namespace.yaml`, `k8s/notification-postgres/`, `k8s/notification-service/`, `k8s/postgres/`, left over from before the Kustomize restructuring. I verified exhaustively that nothing references the flat paths any more: every `kustomization.yaml` resource list, every deploy script (`deploy-kind.sh`, `deploy-docker-desktop.sh`, `deploy-minikube.sh`, `deploy-aws.sh`), and `k8s/README.md` itself all point only at `k8s/base/*` or `k8s/overlays/*`. Their content was otherwise identical to `k8s/base/*` (byte-identical modulo CRLF line endings) — i.e. a genuinely orphaned, silently-stale duplicate that a future engineer could easily edit by mistake and see no effect.
- **Fix:** moved the whole legacy tree into `k8s/_legacy_unused_manifests/`, with a `README.md` inside explaining exactly why (and confirming nothing applies them) so it's obviously safe to delete outright once reviewed. I did not delete the files outright: this session's automatic action-review layer declined the delete-permission request as an irreversible local destruction, so per its own documented fallback I moved them aside instead — functionally the same cleanup, fully reversible, nothing lost. `k8s/kind-config.yaml` (still genuinely used by `deploy-kind.sh`) and `k8s/README.md` (already correct/current) were left alone.
- **Test:** not applicable (docs/manifest hygiene); the deploy scripts' `kubectl apply -k` targets are unchanged.

---

## 3. Security findings

I reviewed this like an attacker: secrets/credentials, auth/authz, Kubernetes RBAC, arbitrary-target risk, command execution, SSRF, webhook abuse, input handling, path traversal, error/log exposure, GitHub Actions, Docker, Terraform/IAM/security groups.

**Result: no exploitable vulnerability found.** This codebase is unusually deliberate about its security boundaries — nearly every file I read had a comment explaining *why* a particular capability was withheld. Specific things I verified rather than assumed:

- **LLM cannot bypass policy or execute anything directly** (severity: informational/confirmed-safe). Verified `rca.py`'s `apply_llm_response()`: the LLM's root-cause/action must exactly match the deterministic rule engine's own conclusion or it's discarded outright; confidence is clamped. There is no code path from LLM output to a Kubernetes API call, a shell, or a policy bypass — `kubernetes_client.py` has no shell/subprocess call anywhere and the container image doesn't even ship `kubectl`.
- **Command injection via the chaos-scenario runner** (severity: low, verified safe). `routers/chaos_scenarios.py` builds a shell command that runs on the AWS EC2 node via SSM, using an admin-token-gated endpoint. The `scenario` value is checked against a fixed allow-list dict before use; the attacker-influenced `namespace` field is passed through `shlex.quote()` before being embedded in the script. This is correctly injection-safe.
- **SSRF via connector URLs** (severity: informational/confirmed-safe). `chaos_client.py`, `slack_client.py`, `github_client.py` all take operator-configured URLs (Settings/env), never end-user or LLM input. `github_client.get_commit()`'s `sha` parameter — the one piece of data that flows from Kubernetes state (an image tag) into a URL path — is explicitly validated as 7–40 hex characters before use, rejecting anything that isn't a plausible commit SHA.
- **Kubernetes RBAC / blast radius** (severity: informational/confirmed-safe). `REQUIRED_RBAC` in `kubernetes_client.py` is documented and minimal: get/list/patch on deployments, get/list on replicasets/pods/events/services, get on pod logs — explicitly no create/delete/update/watch/exec, and everything is namespace-scoped (Role, not ClusterRole). The Policy Engine independently deny-lists `kube-system`/`kube-public`/`kube-node-lease` and any Postgres deployment before any allow-list is even consulted.
- **GitHub Actions / OIDC** (severity: informational/confirmed-safe). No static AWS credentials anywhere. The OIDC trust policy in `infra/terraform/github_oidc.tf` checks both `aud` and a `sub` pinned to specific branches/environment of one named repository — a common misconfiguration (missing `sub` restriction, or a wildcard in the wrong place) that would let any GitHub Actions workflow in the world assume the role is explicitly guarded against here. CI's own IAM policy excludes `ecr:BatchDeleteImage`/`ecr:DeleteRepository` (so CI can't destroy rollback targets) and scopes `ssm:SendCommand` to one specific instance ARN and one specific SSM document, not `"*"`. Every job's `permissions:` block is minimal (`contents: read`, `id-token: write` only, added only where needed).
- **Terraform network/IAM exposure** (severity: informational/confirmed-safe). No SSH (no inbound 22, no `key_name` on the instance at all). No Kubernetes API port (6443) open to the internet — reachable only from Sentinel's own security group by reference, never a CIDR. IMDSv2 is required (`http_tokens = "required"`), closing the classic SSRF-to-instance-credential-theft path. EBS root volume is encrypted. IAM role for the node grants only SSM administration + ECR pull scoped to this project's repositories (no `AdministratorAccess`, no `ec2:*`/`iam:*`).
- **Docker images** (severity: informational/confirmed-safe). All 5 Dockerfiles run as non-root (explicit `useradd`/`USER appuser`), have health checks, and pin base image tags (not `:latest`). `sentinel-ai`'s image deliberately installs no compiler/toolchain and no `kubectl`/shell scripts, specifically so "the LLM cannot run commands" is a structural property of the image, not just application logic.
- **JWT / auth** (severity: informational/confirmed-safe). Both services pass an explicit `algorithms=[...]` allow-list to `jwt.decode` (preventing algorithm-confusion attacks), and Sentinel's admin JWT uses a separate secret *and* a separate `aud` claim from citizen-service's, so a citizen-service token can never be replayed against Sentinel's admin API.
- **Weak default secrets — investigated, not a bug.** `citizen-service/app/core/config.py` defaults `jwt_secret`/`database_password` to the guessable literal `"change_me"`, and `docker-compose.yml`/`.env.example` mirror the same "sentinal"-style demo password. I initially flagged this as a finding, but traced it fully: this is the same deliberate, explicitly-documented "committed demo secret for a throwaway local cluster" convention used consistently across `docker-compose.yml`, `k8s/overlays/local/secrets/*`, and `.env.example` — and the actual production path (`k8s/overlays/aws/`) generates real secrets from gitignored `.env` files and has *no* fallback, so `kubectl apply -k k8s/overlays/aws` fails closed with "no such file or directory" if they weren't generated. Changing citizen-service's code-level default alone, without touching this whole pattern, would be a speculative, inconsistent change against the brief's own instruction — so I left it as-is and record it here for visibility rather than "fixing" something that isn't actually broken.
- **File-upload path traversal — no code path exists yet.** `upload_dir` is defined in `citizen-service`'s config and a `storage_path` column exists on the `Document` model, but I confirmed by search that no router or service currently implements a file-upload endpoint — this is unused scaffolding for a not-yet-built feature, not a live vulnerability. Nothing to fix.
- **Minor, non-security nit (not fixed, low value):** the `deploy-to-sentinel`/`smoke-test` jobs in `ci-cd.yml` use `aws-actions/configure-aws-credentials@v4` while every other job uses `@v5`; harmless (v4 isn't vulnerable) but worth aligning next time that file is touched.

---

## 4. CI/CD

**What failed:** the `test-sentinel-ai` job (`pip install -r requirements.txt && python -m pytest -v` on Python 3.12).

**Why:** two independent, pre-existing bugs in the source, not anything about the CI configuration itself — reproduced locally with the exact same command the workflow runs:
1. `app/main.py` imports a router module, `app.routers.activity`, that did not exist → `ImportError` at collection time → every single test in the suite failed to even collect (12 explicit failures + 107 collection errors).
2. Once that import was fixed, a second, previously-masked bug surfaced: `orchestrator.py` reads an `Evidence` field, `init_container_logs`, that was never defined or populated → `AttributeError` in the 12 tests that actually reach investigation.

**What was changed:** see Bugs 2.1–2.3 above (new `app/routers/activity.py`, new `Evidence.init_container_logs` field + collector in `investigation.py`). No test was disabled, skipped, marked as an allowed failure, or weakened — both were genuine application bugs, fixed at the root.

**Final result:** the exact CI command reproduced clean on both the sandboxed copy and, after writing all fixes back, on the user's real checkout: **474 passed, 0 failed** for `sentinel-ai` (up from 12 failed / 107 errors).

**Docker build:** I did not have a working Docker daemon in either the audit sandbox or the reachable shell on the user's machine, so I was not able to build and run the container per the brief's "if practical" allowance. I did confirm `python -c "import app.main"` succeeds cleanly against the fixed tree (the same import path the Dockerfile's `CMD` exercises at boot), and the Dockerfile itself is unchanged and was reviewed for correctness (section 3).

---

## 5. Test results

Run directly against the user's real checkout (`C:\Users\moaza\OneDrive\Documents\Sentinel-SRE-main`) after all fixes were written back, using a fresh venv per service (SQLite in-memory for citizen-service/notification-service — no external Postgres needed):

| Service | Before this audit | After fixes |
|---|---|---|
| sentinel-ai | 12 failed, 351 passed, 107 errors | **474 passed, 0 failed** |
| citizen-service | (already passing) | **37 passed, 0 failed** |
| notification-service | (already passing) | **24 passed, 0 failed** |
| **Total** | | **535 passed, 0 failed** |

(One pytest run against the mounted OneDrive path hit a `RecursionError` purely in pytest's own temp-directory cleanup, caused by how OneDrive's sync reparse points interact with `shutil.rmtree`'s symlink walk — not a code or test bug. Re-running with `--basetemp` pointed outside the synced folder avoided it entirely and is reflected in the numbers above.)

---

## 6. Remaining issues

- **Docker image build not verified.** No working Docker daemon was reachable from this session (neither the cloud audit sandbox nor the device shell had one). The application boot path (`import app.main`) was verified directly instead; the Dockerfiles themselves were reviewed and are unchanged.
- **k8s legacy manifests moved, not deleted.** `k8s/_legacy_unused_manifests/` still exists on disk (with a README explaining why); this session's own safety layer declined permission to delete files on your machine outright. It's safe to delete that directory once you've had a chance to glance at it — nothing references it.
- **Citizen-service's `jwt_secret`/`database_password` weak defaults** are unchanged (see section 3) — investigated and judged to be consistent, intentional local-dev convenience rather than a bug, but flagged here in case you'd like it changed anyway now that it's been surfaced explicitly.
- **Not independently re-verified end-to-end against a live cluster.** Everything above was validated by unit/integration tests with fakes/injected clients (matching how the existing suite is built) and direct code reading, not against a real Kubernetes API server, Prometheus, or the Groq API. The repo's own `HONESTY NOTE` in `kubernetes_client.py` already flags this as untested against a real cluster; that remains true after this audit, and a first real run should still be done with `DRY_RUN=true` as that note recommends.
- **Sentinel GUI v1.3 / Phase 2–4 work** was explicitly out of scope per your instructions and was not touched, beyond confirming that nothing currently broken in `sentinel-ai`'s existing API blocks it.
