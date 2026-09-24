"""
Sentinel — autonomous SRE agent for the Digital Citizen Services Portal.

Listens on port 8080. Receives Alertmanager webhooks and runs the full
incident lifecycle:

    DETECTION -> INVESTIGATION -> CORRELATION -> ROOT CAUSE ANALYSIS
    -> REMEDIATION DECISION -> POLICY CHECK -> AUTONOMOUS EXECUTION
    -> RECOVERY VALIDATION -> DOCUMENTATION -> NOTIFICATION -> LEARNING

with re-investigation on remediation failure, and escalation when no safe
action remains.

### The architectural boundary, stated once, here

    LLM  ->  Decision Engine  ->  Policy Engine  ->  Remediation Engine  ->  K8s

Read that left to right and note what each arrow is:

* The LLM produces a *structured recommendation* — a root cause from a fixed
  enum, a confidence float, an action name from a fixed enum, and prose. It is
  reached from exactly one function (`rca.enrich_with_llm`) and its output
  passes through one validator (`rca.apply_llm_response`) which discards any
  action or root cause that does not match what the deterministic rules
  already concluded. The LLM has no tools, no function-calling, no shell, no
  kubectl, no Kubernetes credentials, and no network path to the cluster.
* The Decision Engine turns a hypothesis into an ordered list of candidate
  actions. Pure Python, static tables.
* The Policy Engine authorises or denies each candidate against allow-lists,
  frozen deny-lists, confidence thresholds, action caps and cooldowns. Pure,
  deny-by-default.
* The Remediation Engine is the only module that writes to the cluster. It
  refuses to act without an authorisation and re-validates the target against
  its own frozen allow-list before every write.

There is no `subprocess`, no `os.system`, no `eval`, and no kubectl binary in
the image. "The LLM cannot run commands" is a property of the code structure,
not a promise in a prompt.

### Autonomy

DRY_RUN defaults to false. Rollback has no human approval gate — by design.
The safety mechanism is the Policy Engine, not a human clicking approve. See
app/lifecycle/policy.py.

### Untested against a live cluster

Honest note: nothing in this service has run against a real Kubernetes API
server. The Kubernetes write paths (restart annotation, template rollback,
replica patch) mirror what kubectl does but have not been executed. Run the
first real incident with DRY_RUN=true and read the logged patch bodies.

### Sentinel SRE Control Center (GUI)

`app/routers/{auth,dashboard,actions,performance,meta,feedback}.py` back the
GUI described in docs/sentinel-integration.md. `feedback.py` writes to its
own `incident_feedback` table only — it never touches an incident's own
record, the policy engine, or the decision engine, so a diagnosis or
remediation feedback submission cannot change Sentinel's behavior toward
this or any future incident (see that router's module docstring). The rest
are strictly read-only views over the same `store`/`context` this module
already builds — none of them call the Kubernetes client, the Policy
Engine, or the Remediation Engine.
`incidents` and `environments` are now gated behind the same admin JWT
(see app/core/deps.py) since both expose operationally sensitive detail;
`alerts` (the Alertmanager webhook) and `chaos_scenarios` (its own
pre-existing shared-secret gate) are unchanged.
"""
import asyncio
import logging
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.core.config import settings
from app.core.events import EventBus
from app.core.logging_config import configure_logging
from app.core.security import hash_password
from app.domain.environment import Environment
from app.lifecycle import ai_admin, k8s_watch, policy_admin, rca_admin, remediation_admin
from app.lifecycle.incident_manager import IncidentManager
from app.lifecycle.orchestrator import Orchestrator, build_context
from app.routers import (
    actions,
    activity,
    alerts,
    auth,
    authorizations,
    chaos_scenarios,
    config,
    dashboard,
    environments,
    evaluation,
    events,
    feedback,
    health,
    incidents,
    logs,
    meta,
    performance,
    reinvestigate,
)
from app.store.sqlite_store import SQLiteStore

_persistent_log_handler = configure_logging(
    service_name=settings.service_name,
    log_dir=settings.sentinel_log_dir_resolved,
    log_max_bytes=settings.sentinel_log_max_bytes,
    log_backup_count=settings.sentinel_log_backup_count,
)
logger = logging.getLogger(__name__)


def _load_or_create_persisted_jwt_secret(settings_obj: Any) -> str:
    """Fallback for an unset SENTINEL_JWT_SECRET that survives a container
    restart, not just this process's lifetime.

    Setting SENTINEL_JWT_SECRET explicitly (e.g. via the EC2 topology's
    `extra-env` SSM parameter, or the in-cluster `sentinel-secret` Secret)
    is still the right thing to do for anything beyond a demo, and remains
    fully respected — this only runs when that was left unset. Previously
    an unset secret meant a fresh random value EVERY process start, which
    logs every admin out on every restart even though the incident history
    itself now correctly survives one (see SQLiteStore persistence).
    Persisting the generated value next to the SQLite DB — same volume,
    same lifetime, same "must survive a restart" property — closes that
    gap without requiring any new configuration.

    Falls back to a process-lifetime-only secret (the old behaviour) if the
    volume is not writable for some reason, exactly like SQLiteStore's own
    in-memory fallback: degrade loudly, never crash on this.
    """
    secret_path = Path(settings_obj.sentinel_db_path).parent / ".jwt_secret"
    try:
        if secret_path.exists():
            existing = secret_path.read_text().strip()
            if existing:
                logger.info(
                    "sentinel_jwt_secret_loaded_from_disk",
                    extra={"detail": "Reusing the persisted JWT secret; existing GUI "
                           "sessions remain valid across this restart."},
                )
                return existing

        secret_path.parent.mkdir(parents=True, exist_ok=True)
        generated = secrets.token_urlsafe(48)
        secret_path.write_text(generated)
        secret_path.chmod(0o600)
        logger.warning(
            "sentinel_jwt_secret_generated_and_persisted",
            extra={
                "detail": "SENTINEL_JWT_SECRET was not set; generated one and saved it "
                "to the persistent volume so GUI sessions survive future restarts. Set "
                "SENTINEL_JWT_SECRET explicitly instead for anything beyond a demo.",
            },
        )
        return generated
    except OSError as exc:
        generated = secrets.token_urlsafe(48)
        logger.warning(
            "sentinel_jwt_secret_generated_process_only",
            extra={
                "detail": "SENTINEL_JWT_SECRET was not set and the persistent volume "
                "was not writable, so this secret is process-lifetime only. Every GUI "
                "session will be invalidated on the next restart.",
                "error_detail": str(exc)[:200],
            },
        )
        return generated


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Wire the object graph once, at startup.

    Deliberately loud about the mode it is running in. An operator reading the
    first ten lines of the log should be able to tell whether this Sentinel
    will actually touch the cluster, whether it can reach Kubernetes, and
    whether the LLM is in play — without exec'ing into the pod.
    """
    store = SQLiteStore(settings.sentinel_db_path)
    store.connect()

    # Bootstrap the Phase-1 demo Environment if nothing has been registered
    # yet (e.g. via POST /environments). This is what lets Sentinel keep
    # booting with zero extra configuration straight from env vars, while
    # everything downstream (SentinelContext, connectors) is built from an
    # Environment record rather than global settings — see
    # app/domain/environment.py's module docstring.
    existing = store.list_environments()
    if existing:
        environment = Environment(**existing[0])
        logger.info(
            "environment_loaded_from_store",
            extra={"environment_id": environment.id, "customer_id": environment.customer_id},
        )
    else:
        environment = Environment.bootstrap_from_settings(settings)
        store.upsert_environment(environment.to_dict())
        logger.info(
            "environment_bootstrapped_from_settings",
            extra={"environment_id": environment.id, "customer_id": environment.customer_id,
                   "kubernetes_mode": environment.kubernetes.mode},
        )

    event_bus = EventBus()
    ctx = build_context(settings, store, environment, event_bus=event_bus)
    orchestrator = Orchestrator(ctx)

    # Separate bus for log-line streaming (Sentinel Logs GUI page). Kept
    # distinct from `event_bus` above deliberately: log volume is much
    # higher and bursty (a busy lifecycle run can emit dozens of lines),
    # and events.py's per-subscriber queue is sized/tuned for occasional
    # incident-state signals, not a live log tail — mixing the two would
    # mean either logs drowning out incident events in a shared queue, or
    # incident events being dropped by a slow log-tail subscriber. Same
    # EventBus class, same "simple in-process pub/sub, swappable later"
    # design (see core/events.py), just a second instance.
    log_bus = EventBus(max_queue_size=500)
    if _persistent_log_handler is not None:
        _persistent_log_handler.set_bus(log_bus)

    # ---- Sentinel Administration & Tuning Center: reload live policy -----
    # ---- overrides ---------------------------------------------------
    # `PolicyConfig` (ctx.policy.config) was just built fresh from
    # `settings`-derived defaults. Any change an admin previously applied
    # via POST /api/config/policy/apply is stored in `config_overrides`
    # (see app/store/sqlite_store.py) precisely so it survives this
    # restart — reapply it now, before Sentinel processes its first
    # incident, using the SAME validated apply path a live request uses
    # (app/lifecycle/policy_admin.py), not a separate ad hoc assignment.
    stored_policy_overrides = store.get_config_overrides("policy")
    override_errors = policy_admin.reload_stored_overrides(
        ctx,
        stored_policy_overrides,
        ctx.policy.config.denied_deployments,
        ctx.policy.config.denied_namespaces,
    )
    if override_errors:
        # A bound tightened since this override was saved (e.g. a Sentinel
        # upgrade lowered MAX_REPLICAS_CEILING) — fail loud, not silently
        # ignore a safety-relevant stored override.
        logger.error(
            "stored_policy_overrides_invalid",
            extra={"errors": override_errors, "overrides": stored_policy_overrides},
        )
    elif stored_policy_overrides:
        logger.info(
            "stored_policy_overrides_applied", extra={"fields": sorted(stored_policy_overrides)}
        )

    stored_rca_overrides = store.get_config_overrides("rca")
    rca_override_errors = rca_admin.reload_stored_overrides(ctx.validator.thresholds, stored_rca_overrides)
    if rca_override_errors:
        logger.error(
            "stored_rca_overrides_invalid",
            extra={"errors": rca_override_errors, "overrides": stored_rca_overrides},
        )
    elif stored_rca_overrides:
        logger.info("stored_rca_overrides_applied", extra={"fields": sorted(stored_rca_overrides)})

    stored_remediation_overrides = store.get_config_overrides("remediation")
    remediation_override_errors = remediation_admin.reload_stored_overrides(
        ctx.remediation, stored_remediation_overrides
    )
    if remediation_override_errors:
        logger.error(
            "stored_remediation_overrides_invalid",
            extra={"errors": remediation_override_errors, "overrides": stored_remediation_overrides},
        )
    elif stored_remediation_overrides:
        logger.info(
            "stored_remediation_overrides_applied",
            extra={"fields": sorted(stored_remediation_overrides)},
        )

    # ai_admin.reload_stored_overrides takes `ctx`, not `ctx.settings` alone
    # (unlike rca/remediation above) — see ai_admin.py's module docstring:
    # applying a stored provider/model/timeout/base_url override must also
    # rebuild ctx.reasoner before Sentinel processes its first incident.
    stored_ai_overrides = store.get_config_overrides("ai")
    ai_override_errors = ai_admin.reload_stored_overrides(ctx, stored_ai_overrides)
    if ai_override_errors:
        logger.error(
            "stored_ai_overrides_invalid",
            extra={"errors": ai_override_errors, "overrides": stored_ai_overrides},
        )
    elif stored_ai_overrides:
        logger.info("stored_ai_overrides_applied", extra={"fields": sorted(stored_ai_overrides)})

    # ---- Sentinel SRE Control Center (GUI) admin auth bootstrap ----------
    # No safe hardcoded secret/password (see core/config.py's field docs):
    # generate one if the operator did not set one, and log it loudly so it
    # is impossible to miss on first boot but never written to a file.
    if not settings.sentinel_jwt_secret:
        settings.sentinel_jwt_secret = _load_or_create_persisted_jwt_secret(settings)

    if store.count_admins() == 0:
        bootstrap_password = settings.sentinel_admin_password or secrets.token_urlsafe(16)
        admin_id = f"admin-{uuid.uuid4().hex[:12]}"
        store.create_admin(
            admin_id=admin_id,
            username=settings.sentinel_admin_username,
            password_hash=hash_password(bootstrap_password),
        )
        if settings.sentinel_admin_password:
            logger.info(
                "sentinel_gui_admin_bootstrapped",
                extra={"username": settings.sentinel_admin_username},
            )
        else:
            # Only place this ever appears. Anyone who needs it must read it
            # from the startup log for this one boot.
            logger.warning(
                "sentinel_gui_admin_bootstrapped_with_generated_password",
                extra={
                    "username": settings.sentinel_admin_username,
                    "generated_password": bootstrap_password,
                    "detail": "SENTINEL_ADMIN_PASSWORD was not set; generated a "
                    "one-time password for the bootstrap admin account, shown here "
                    "once. Log in and treat this as sensitive.",
                },
            )

    app.state.settings = settings
    app.state.store = store
    app.state.environment = environment
    app.state.context = ctx
    app.state.orchestrator = orchestrator
    app.state.event_bus = event_bus
    app.state.log_bus = log_bus
    incident_manager = IncidentManager(
        store, settings, event_bus=event_bus, orchestrator=orchestrator
    )
    ctx.incident_manager = incident_manager
    app.state.incident_manager = incident_manager
    health.register_runtime(store=store, k8s=ctx.k8s, remediation=ctx.remediation)

    logger.info(
        "sentinel_started",
        extra={
            "version": settings.version,
            "mode": "dry_run" if settings.dry_run else "autonomous",
            "customer_id": environment.customer_id,
            "environment_id": environment.id,
            "kubernetes_mode": environment.kubernetes.mode,
            "kubernetes_available": ctx.k8s.available,
            "llm": "enabled" if settings.llm_enabled else "rule_based_only",
            "llm_provider": settings.llm_provider if settings.llm_enabled else None,
            "allowed_namespaces": settings.allowed_namespaces_list,
            "allowed_deployments": settings.allowed_deployments_list,
            "denied_deployments": list(settings.denied_deployments_frozen),
            "github_issues": environment.github.is_enabled(),
            "slack_notifications": settings.slack_enabled,
            "chaos_control_plane": bool(settings.chaos_admin_token),
            "prometheus_url": environment.prometheus.url,
            "loki_url": environment.loki.url,
        },
    )
    if not settings.llm_enabled:
        logger.info(
            "llm_disabled",
            extra={
                "detail": "No LLM provider is configured (see LLM_PROVIDER / "
                "OPENAI_API_KEY / GEMINI_API_KEY). Root cause analysis will be "
                "entirely rule-based. This is a fully supported mode — the LLM only "
                "ever enriches the narrative and adjusts confidence within a small "
                "clamped range; it can never choose an action."
            },
        )
    if not ctx.k8s.available:
        logger.warning(
            "kubernetes_unavailable_at_startup",
            extra={
                "detail": "Sentinel cannot read Deployments or execute restart / "
                "rollback / scale actions. Chaos-fault resets still work (plain "
                "HTTP). Every other incident will escalate. Check "
                "KUBERNETES_MODE and the corresponding credentials, or the "
                "ServiceAccount/RBAC Role if running in_cluster.",
                "error_detail": ctx.k8s.init_error,
            },
        )
    if settings.dry_run:
        logger.warning(
            "dry_run_enabled",
            extra={
                "detail": "DRY_RUN=true: Sentinel will decide and authorise actions "
                "but will NOT apply them to the cluster. Nothing will actually be "
                "remediated."
            },
        )

    # Incidents the previous process left mid-lifecycle are classified here
    # (resume once if nothing irreversible happened, else ESCALATED as
    # INTERRUPTED) — persisted state is the truth, nothing is silently lost.
    incident_manager.recover_interrupted()

    # Independent Kubernetes desired-vs-actual watch — see
    # lifecycle/k8s_watch.py's module docstring for why this exists
    # (Alertmanager alone cannot catch a Deployment with zero Pods, since
    # there is no scrape target for `up` to be 0 on). Feeds the SAME
    # IncidentManager path a real Alertmanager webhook uses.
    k8s_watch_task = None
    if settings.k8s_watch_enabled:
        k8s_watch_task = asyncio.create_task(
            k8s_watch.run_k8s_watch(
                ctx,
                incident_manager,
                environment,
                poll_interval_seconds=settings.k8s_watch_poll_interval_seconds,
                debounce_seconds=settings.k8s_watch_debounce_seconds,
                expected_zero_annotation=settings.k8s_watch_expected_zero_annotation,
            ),
            name="k8s-watch",
        )

    try:
        yield
    finally:
        if k8s_watch_task is not None:
            k8s_watch_task.cancel()
            try:
                await k8s_watch_task
            except asyncio.CancelledError:
                pass
        await incident_manager.shutdown()
        store.close()
        logger.info("sentinel_stopped")


app = FastAPI(
    title="Digital Citizen Services Portal — Sentinel SRE Agent",
    description=(
        "Autonomous incident lifecycle: detect, investigate, correlate, analyse, "
        "decide, policy-check, remediate, validate, document, notify, learn. "
        "The LLM analyses evidence and returns a structured recommendation; all "
        "authorisation and all cluster writes are deterministic Python behind an "
        "allow-list."
    ),
    version=settings.version,
    lifespan=lifespan,
)


# CORS for the Sentinel SRE Control Center GUI only — this API now issues
# bearer tokens, so no wildcard origin (see core/config.py's
# sentinel_gui_origins docs). The Alertmanager webhook is never called from
# a browser, so it is unaffected by this either way. The chaos-scenarios
# endpoint IS called from the browser (sentinel-gui's DemoChaosPage.jsx),
# same-origin via sentinel-gui/nginx.conf's proxy — it doesn't need CORS
# either, but for a different reason: it's never cross-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.sentinel_gui_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(alerts.router)
app.include_router(auth.router)
app.include_router(incidents.router)
app.include_router(feedback.router)
app.include_router(config.router)
app.include_router(authorizations.router)
app.include_router(environments.router)
app.include_router(chaos_scenarios.router)
app.include_router(evaluation.router)
app.include_router(dashboard.router)
app.include_router(actions.router)
app.include_router(performance.router)
app.include_router(meta.router)
app.include_router(events.router)
app.include_router(logs.router)
app.include_router(activity.router)
app.include_router(reinvestigate.router)


@app.get("/metrics")
def metrics() -> Response:
    """Sentinel's own metrics.

    Hand-rolled rather than via prometheus-fastapi-instrumentator (which the
    app services use) because Sentinel's inbound HTTP surface is one webhook
    and three read endpoints — per-handler request metrics would be noise, and
    keeping /metrics to only `sentinel_*` series means a Grafana panel can
    wildcard `sentinel_.*` safely.

    Consequence worth knowing: Sentinel does NOT export
    `http_requests_total`, so the existing `HighHTTPErrorRate` and
    `ServiceDown` alert rules will not fire for Sentinel itself. Sentinel does
    not watch Sentinel.
    """
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
