"""
Chaos control-plane client.

Both citizen-service and notification-service expose:
    GET  /api/chaos/status
    POST /api/chaos/fault
    POST /api/chaos/reset
authenticated by the `X-Chaos-Token` header, value from CHAOS_ADMIN_TOKEN.

Two facts that shape this module:

1. **A wrong or missing token returns 404, not 401.** The app services do
   that on purpose (the endpoint should be invisible without the token). It
   means a 404 from here is ambiguous: bad token, chaos disabled
   (`CHAOS_MODE=false`), or genuinely no such route. We report all three the
   same way and say so, because guessing would be worse.

2. **Chaos state is per-pod, in-memory.** A POST through the Service VIP
   lands on whichever replica kube-proxy picks. On AWS these Deployments run
   at 1 replica so this is deterministic, but the reset is still not
   trustworthy on its own — `verify_cleared()` re-checks the `chaos_*` gauges
   in Prometheus *per `kubernetes_pod_name`* before we call the fault
   cleared. A 200 from reset means "some pod cleared its state", not "the
   fault is gone".

Sentinel only ever calls `/reset` and `/status`. It never calls `/fault` —
Sentinel's job is to remove faults, not create them, and not having the
injection call in the codebase means there is no path by which a confused
LLM-driven flow could inject one.

A plain `/api/chaos/reset` clears **everything** in the chaos state, which
includes the two fields another agent is adding to `POST /api/chaos/fault`
(`cpu_burn: bool`, `memory_leak_mb: int`). No change is needed here to
support resetting those — that is precisely why the reset action calls the
blanket reset rather than posting a fault payload of zeroes. `verify_cleared`
does not check gauges for those two, because their metric names are not
confirmed yet; see the comment there.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class ChaosResetOutcome:
    """Result of a reset attempt. Not an exception — the caller decides."""

    def __init__(
        self,
        succeeded: bool,
        http_status: int | None,
        detail: str,
        state: dict[str, Any] | None = None,
    ) -> None:
        self.succeeded = succeeded
        self.http_status = http_status
        self.detail = detail
        self.state = state or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "succeeded": self.succeeded,
            "http_status": self.http_status,
            "detail": self.detail,
            "state": self.state,
        }


class ChaosClient:
    def __init__(self, token: str, timeout: float = 5.0) -> None:
        self.token = token
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.token.strip())

    def _headers(self) -> dict[str, str]:
        return {"X-Chaos-Token": self.token}

    async def status(self, base_url: str) -> dict[str, Any] | None:
        """GET /api/chaos/status. None on any failure (including 404)."""
        if not self.configured:
            return None
        url = f"{base_url.rstrip('/')}/api/chaos/status"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, headers=self._headers())
        except httpx.HTTPError as exc:
            logger.warning(
                "chaos_status_unreachable",
                extra={"target_url": url, "error_detail": str(exc)[:200]},
            )
            return None
        if resp.status_code != 200:
            logger.warning(
                "chaos_status_rejected",
                extra={"target_url": url, "status_code": resp.status_code},
            )
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    async def reset(self, base_url: str) -> ChaosResetOutcome:
        """POST /api/chaos/reset — clears the entire chaos state for one pod.

        Never raises. A failure here is a failed remediation attempt, which
        the orchestrator handles by re-investigating and trying the next safe
        action; an exception would abort the lifecycle mid-incident.
        """
        if not self.configured:
            return ChaosResetOutcome(
                succeeded=False,
                http_status=None,
                detail=(
                    "CHAOS_ADMIN_TOKEN is not set, so Sentinel cannot authenticate "
                    "to the chaos control plane. This action is unavailable."
                ),
            )
        url = f"{base_url.rstrip('/')}/api/chaos/reset"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, headers=self._headers())
        except httpx.HTTPError as exc:
            return ChaosResetOutcome(
                succeeded=False,
                http_status=None,
                detail=f"chaos reset request failed: {str(exc)[:200]}",
            )

        if resp.status_code == 404:
            # Ambiguous by design on the server side — see module docstring.
            return ChaosResetOutcome(
                succeeded=False,
                http_status=404,
                detail=(
                    "chaos reset returned 404. The app services return 404 (not 401) "
                    "for a wrong/missing X-Chaos-Token and also when CHAOS_MODE is "
                    "false, so this means one of: bad token, chaos disabled, or route "
                    "absent. Cannot distinguish from the response."
                ),
            )
        if resp.status_code != 200:
            return ChaosResetOutcome(
                succeeded=False,
                http_status=resp.status_code,
                detail=f"chaos reset returned HTTP {resp.status_code}",
            )

        try:
            state = resp.json()
        except ValueError:
            state = {}
        return ChaosResetOutcome(
            succeeded=True,
            http_status=200,
            detail="chaos reset accepted (per-pod; verify via chaos_* gauges)",
            state=state if isinstance(state, dict) else {},
        )


def verify_cleared(chaos_state: dict[str, dict[str, float]]) -> tuple[bool, list[str]]:
    """Check the per-pod `chaos_*` gauges for any remaining fault.

    `chaos_state` is what PrometheusClient.chaos_state() returns:
    ``{pod_name: {metric_name: value}}``.

    Returns (cleared, offending_descriptions). Cleared means every pod we can
    see reports latency 0, error_rate 0, db_failure 0 and (where present)
    notification_failure_rate 0.

    An EMPTY dict returns (False, [...]) rather than (True, []). No visible
    gauges means we cannot see the pods at all — possibly because the reset
    hit the wrong replica and the faulted one stopped being scraped. "I can't
    see it" must never be reported as "it's fixed"; that is the single most
    dangerous default an autonomous remediator can have.

    Not checked here: the `cpu_burn` / `memory_leak_mb` faults another agent
    is adding. A blanket `/api/chaos/reset` clears them server-side, but their
    gauge names are not confirmed, and inventing a metric name would produce
    an always-empty query that silently passes. CPU and memory are instead
    validated through `process_cpu_seconds_total` /
    `process_resident_memory_bytes` in the validation phase, which observes
    the *effect* rather than the flag.
    """
    if not chaos_state:
        return False, [
            "no chaos_* gauges visible in Prometheus for this app; cannot confirm "
            "the fault cleared (the chaos gauges are always exported, so an absent "
            "series means the pod is not being scraped)"
        ]

    offenders: list[str] = []
    for pod, metrics in chaos_state.items():
        if metrics.get("chaos_db_failure", 0.0) > 0:
            offenders.append(f"{pod}: chaos_db_failure still 1")
        if metrics.get("chaos_error_rate", 0.0) > 0:
            offenders.append(
                f"{pod}: chaos_error_rate still {metrics['chaos_error_rate']}"
            )
        if metrics.get("chaos_latency_ms", 0.0) > 0:
            offenders.append(
                f"{pod}: chaos_latency_ms still {metrics['chaos_latency_ms']}"
            )
        rate = metrics.get("chaos_notification_failure_rate")
        if rate is not None and rate > 0:
            offenders.append(f"{pod}: chaos_notification_failure_rate still {rate}")

    return (not offenders), offenders
