"""
Environment registration API (spec section 27).

  POST /environments                      — register a remote environment
  GET  /environments                      — list registered environments
  GET  /environments/{id}                 — get one (redacted)
  POST /environments/{id}/test-connection — probe each configured connector
  POST /environments/{id}/discover        — basic namespace-scoped discovery

PHASE 1 SIMPLIFICATION, stated once here: Sentinel runs exactly one active
`SentinelContext` at a time (`request.app.state.context`). Registering an
environment via POST immediately (re)builds that context and makes the new
environment the one the Alertmanager webhook dispatches incidents against —
there is no multi-environment routing yet (see
app/domain/environment.py's module docstring for what that would take).
This means POSTing a second environment REPLACES the active one; it does not
run alongside it. That is an explicit, documented limitation, not an
oversight — see the top-level integration doc's "Known limitations".

Credentials never appear in a response body from this router. Every
response goes through `Environment.to_public_dict()`.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.clients.kubernetes_client import KubernetesClient
from app.domain.environment import (
    ApplicationProfile,
    AWSConnectionConfig,
    Environment,
    GitHubConnectionConfig,
    KubernetesConnectionConfig,
    LokiConnectionConfig,
    PrometheusConnectionConfig,
)
from app.lifecycle.orchestrator import Orchestrator, build_context

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/environments", tags=["environments"])


class RegisterEnvironmentRequest(BaseModel):
    """Request body for POST /environments — mirrors Environment but without
    server-assigned fields (id/created_at)."""

    name: str
    customer_id: str
    kubernetes: KubernetesConnectionConfig
    prometheus: PrometheusConnectionConfig
    loki: LokiConnectionConfig
    github: GitHubConnectionConfig = Field(default_factory=GitHubConnectionConfig)
    aws: AWSConnectionConfig = Field(default_factory=AWSConnectionConfig)
    application: ApplicationProfile


def _require_store(request: Request) -> Any:
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="incident store is not ready",
        )
    return store


@router.post("", status_code=status.HTTP_201_CREATED)
def register_environment(
    body: RegisterEnvironmentRequest, request: Request
) -> dict[str, Any]:
    store = _require_store(request)
    environment = Environment(
        customer_id=body.customer_id,
        name=body.name,
        kubernetes=body.kubernetes,
        prometheus=body.prometheus,
        loki=body.loki,
        github=body.github,
        aws=body.aws,
        application=body.application,
    )
    store.upsert_environment(environment.to_dict())

    # Activate it — see module docstring for the Phase 1 single-active-
    # environment simplification this implies.
    settings_obj = request.app.state.settings
    ctx = build_context(settings_obj, store, environment)
    orchestrator = Orchestrator(ctx)
    request.app.state.environment = environment
    request.app.state.context = ctx
    request.app.state.orchestrator = orchestrator

    logger.info(
        "environment_registered",
        extra={
            "environment_id": environment.id,
            "customer_id": environment.customer_id,
            "kubernetes_mode": environment.kubernetes.mode,
            "kubernetes_available": ctx.k8s.available,
        },
    )
    return environment.to_public_dict()


@router.get("")
def list_environments(request: Request) -> dict[str, Any]:
    store = _require_store(request)
    records = store.list_environments()
    return {
        "count": len(records),
        "environments": [Environment(**r).to_public_dict() for r in records],
    }


@router.get("/{environment_id}")
def get_environment(environment_id: str, request: Request) -> dict[str, Any]:
    store = _require_store(request)
    record = store.get_environment(environment_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="environment not found"
        )
    return Environment(**record).to_public_dict()


@router.post("/{environment_id}/test-connection")
async def test_connection(environment_id: str, request: Request) -> dict[str, Any]:
    """Probe each configured connector. Never raises on a probe failure —
    a failed connector is a normal, informative result, not an error
    response; the endpoint itself only 404s if the environment is unknown.
    """
    store = _require_store(request)
    record = store.get_environment(environment_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="environment not found"
        )
    environment = Environment(**record)
    results: dict[str, Any] = {}

    # ---- kubernetes -------------------------------------------------
    k8s = KubernetesClient(connection=environment.kubernetes)
    k8s.initialise()
    results["kubernetes"] = {
        "ok": k8s.available,
        "mode": environment.kubernetes.mode,
        "detail": None if k8s.available else k8s.init_error,
    }

    # ---- prometheus ---------------------------------------------------
    results["prometheus"] = await _probe_http(
        f"{environment.prometheus.url.rstrip('/')}/-/healthy",
        environment.prometheus.bearer_token,
    )

    # ---- loki -----------------------------------------------------------
    results["loki"] = await _probe_http(
        f"{environment.loki.url.rstrip('/')}/ready",
        environment.loki.bearer_token,
    )

    # ---- github (optional) ---------------------------------------------
    if environment.github.is_enabled():
        headers = {"Authorization": f"Bearer {environment.github.token}"}
        url = f"https://api.github.com/repos/{environment.github.repository}"
        probe = await _probe_http(url, None, headers=headers)
        results["github"] = probe
    else:
        results["github"] = {"ok": None, "detail": "not configured"}

    # ---- aws (optional, connector not yet wired into evidence collection)
    results["aws"] = {
        "ok": None,
        "detail": (
            "configured, connectivity probe not implemented in this prototype"
            if environment.aws.is_enabled()
            else "not configured"
        ),
    }

    overall_ok = results["kubernetes"]["ok"] and results["prometheus"]["ok"]
    return {"environment_id": environment_id, "ok": bool(overall_ok), "connectors": results}


async def _probe_http(
    url: str, bearer_token: str | None, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    hdrs = dict(headers or {})
    if bearer_token:
        hdrs["Authorization"] = f"Bearer {bearer_token}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=hdrs)
        return {"ok": resp.status_code < 400, "status_code": resp.status_code, "detail": None}
    except httpx.HTTPError as exc:
        return {"ok": False, "status_code": None, "detail": str(exc)[:200]}


@router.post("/{environment_id}/discover")
async def discover_environment(environment_id: str, request: Request) -> dict[str, Any]:
    """Basic namespace-scoped discovery (spec section 20): deployments,
    services, pods currently in the environment's configured namespace.
    Deliberately shallow — no dependency graph, no service-mesh topology.
    """
    store = _require_store(request)
    record = store.get_environment(environment_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="environment not found"
        )
    environment = Environment(**record)

    k8s = KubernetesClient(connection=environment.kubernetes)
    if not k8s.initialise():
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"could not reach Kubernetes for this environment: {k8s.init_error}",
        )

    namespace = environment.kubernetes.namespace
    deployments = await k8s.list_deployments(namespace)
    services = await k8s.list_services(namespace)
    pods = await k8s.list_pods(namespace)
    return {
        "environment_id": environment_id,
        "namespace": namespace,
        "deployments": deployments,
        "services": services,
        "pod_count": len(pods),
    }
