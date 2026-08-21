"""
Health probes.

Same contract as the app services so the same probe configuration works:
  * `/healthz` — liveness. Always 200 while the process is alive. Must never
    touch a dependency: if this probed Prometheus, a Prometheus outage would
    make Kubernetes restart Sentinel, which is exactly when you want Sentinel
    running.
  * `/readyz` — readiness, with a JSON `status` field.

Note the readiness semantics chosen for Sentinel specifically: Kubernetes
being unreachable makes Sentinel **not_ready** (it cannot remediate anything,
so it should not receive webhooks), but Prometheus or Loki being unreachable
only makes it **degraded** (it can still act, with worse evidence, and its
own RCA lowers confidence accordingly). Returning `degraded` with HTTP 200
mirrors what citizen-service does for an unreachable downstream — a
status-code-only check will not notice, which is exactly why the field exists.
"""
from fastapi import APIRouter, Response, status

from app.core.config import settings

router = APIRouter(tags=["health"])

# Set by main.py's lifespan. Module-level rather than app.state so the probes
# stay trivially cheap and cannot themselves fail on a missing attribute.
_runtime: dict[str, object] = {"store": None, "k8s": None}


def register_runtime(store: object, k8s: object) -> None:
    _runtime["store"] = store
    _runtime["k8s"] = k8s


@router.get("/healthz")
def liveness():
    """Liveness probe: process is up. Must never touch a dependency."""
    return {"status": "ok", "version": settings.version}


@router.get("/readyz")
def readiness(response: Response):
    checks: dict[str, str] = {}

    store = _runtime.get("store")
    if store is None:
        checks["store"] = "down"
    else:
        try:
            store.count_open()  # type: ignore[attr-defined]
            checks["store"] = "up"
        except Exception:  # noqa: BLE001
            checks["store"] = "down"

    k8s = _runtime.get("k8s")
    checks["kubernetes"] = "up" if getattr(k8s, "available", False) else "down"

    # Configuration facts, surfaced here so an operator can confirm the mode
    # Sentinel is actually running in without reading logs.
    checks["mode"] = "dry_run" if settings.dry_run else "autonomous"
    checks["llm"] = "enabled" if settings.llm_enabled else "rule_based_only"

    if checks["store"] == "down" or checks["kubernetes"] == "down":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "checks": checks}

    return {"status": "ready", "checks": checks}
