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
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.core.config import settings
from app.core.logging_config import configure_logging
from app.lifecycle.orchestrator import Orchestrator, build_context
from app.routers import alerts, health, incidents
from app.store.sqlite_store import SQLiteStore

configure_logging(service_name=settings.service_name)
logger = logging.getLogger(__name__)


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

    ctx = build_context(settings, store)
    orchestrator = Orchestrator(ctx)

    app.state.settings = settings
    app.state.store = store
    app.state.context = ctx
    app.state.orchestrator = orchestrator
    health.register_runtime(store=store, k8s=ctx.k8s)

    logger.info(
        "sentinel_started",
        extra={
            "version": settings.version,
            "mode": "dry_run" if settings.dry_run else "autonomous",
            "kubernetes_available": ctx.k8s.available,
            "llm": "enabled" if settings.llm_enabled else "rule_based_only",
            "openai_model": settings.openai_model if settings.llm_enabled else None,
            "allowed_namespaces": settings.allowed_namespaces_list,
            "allowed_deployments": settings.allowed_deployments_list,
            "denied_deployments": list(settings.denied_deployments_frozen),
            "github_issues": settings.github_enabled,
            "slack_notifications": settings.slack_enabled,
            "chaos_control_plane": bool(settings.chaos_admin_token),
            "prometheus_url": settings.prometheus_url,
            "loki_url": settings.loki_url,
        },
    )
    if not settings.llm_enabled:
        logger.info(
            "llm_disabled",
            extra={
                "detail": "OPENAI_API_KEY is not set. Root cause analysis will be "
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
                "HTTP). Every other incident will escalate. Check the "
                "ServiceAccount and RBAC Role.",
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

    try:
        yield
    finally:
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

app.include_router(health.router)
app.include_router(alerts.router)
app.include_router(incidents.router)


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
