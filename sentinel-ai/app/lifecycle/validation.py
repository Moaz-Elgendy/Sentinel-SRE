"""
RECOVERY VALIDATION.

An incident is only RESOLVED when this phase says so. That is the whole
premise of an autonomous remediator: without verification, "I restarted it"
is a hope, not a resolution.

### What is checked

1. Deployment `availableReplicas == spec.replicas`
2. Every pod Ready (and none crash-looping)
3. HTTP health endpoint — **parsing the JSON `status` field**, not the status
   code (see below)
4. 5xx error rate below threshold
5. p95 latency below threshold
6. CPU and memory sane
7. All relevant `chaos_*` gauges cleared, **per pod**

### Why the health check parses JSON

`/readyz` on both Python services returns:
  * HTTP 503 + `{"status":"not_ready","checks":{"database":"down"}}` when the
    DB is down, and
  * **HTTP 200** + `{"status":"degraded"}` when notification-service is
    unreachable.

So a downstream outage is completely invisible to a status-code-only check.
Anything that only looked at `resp.status_code == 200` would declare a
half-broken service fully recovered. We parse `status` and treat `degraded`
as a distinct outcome: not fully recovered, but meaningfully different from
`not_ready` — the service is serving traffic, its dependency is not. The
frontend is different again: `GET /healthz` returns the plain text `ok`, not
JSON, so it is handled as a special case.

### Polling, not sleeping

We wait `VALIDATION_SETTLE_SECONDS` once (a rollout needs time to start, and
checking availableReplicas one millisecond after a patch always "passes"
against the old pods), then poll every
`VALIDATION_POLL_INTERVAL_SECONDS` until either everything passes or
`VALIDATION_TIMEOUT_SECONDS` elapses. Blindly sleeping for the full timeout
would add minutes to every successful remediation.

One honest caveat about metric lag: `error_rate` is a `rate(...[5m])`. After
a genuine fix, the 5-minute window still contains the bad minutes, so the
error rate decays rather than snapping to zero. With a default 180s timeout
the rate may still be above threshold when we give up. `_error_rate_ok`
therefore accepts a *recovering* rate — one that has dropped substantially
below the level observed at investigation time — as passing, and says so in
the check detail. Without that, every successful remediation of an error
spike would report a validation timeout.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from app.clients.chaos_client import verify_cleared
from app.clients.kubernetes_client import KubernetesClient
from app.clients.prometheus import PrometheusClient
from app.models.incident import (
    ActionParams,
    Incident,
    ValidationOutcome,
    ValidationReport,
)

logger = logging.getLogger(__name__)

# Fraction of the pre-remediation error rate below which we accept "still
# decaying" as recovered. 0.4 == the rate has dropped by at least 60%.
ERROR_RATE_RECOVERY_FRACTION = 0.4


async def probe_health(
    base_url: str, timeout: float = 5.0, path: str = "/readyz"
) -> dict[str, Any]:
    """Probe a health endpoint and return the parsed `status`.

    Returns ``{"status": str|None, "http_code": int|None, "checks": dict,
    "reachable": bool, "detail": str}``.

    `status` values in this stack: "ready", "degraded", "not_ready", "ok"
    (from /healthz), or "unparsed" when the body is not JSON — which is the
    normal case for the frontend, whose /healthz returns the plain text `ok`.
    """
    url = f"{base_url.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        return {
            "status": None,
            "http_code": None,
            "checks": {},
            "reachable": False,
            "detail": f"health probe to {url} failed: {str(exc)[:160]}",
        }

    body: Any
    try:
        body = resp.json()
    except ValueError:
        # Plain-text body. The frontend returns literally "ok".
        text = (resp.text or "").strip().lower()
        return {
            "status": "ok" if text == "ok" and resp.status_code == 200 else "unparsed",
            "http_code": resp.status_code,
            "checks": {},
            "reachable": True,
            "detail": f"non-JSON health body: {(resp.text or '')[:60]!r}",
        }

    if not isinstance(body, dict):
        return {
            "status": "unparsed",
            "http_code": resp.status_code,
            "checks": {},
            "reachable": True,
            "detail": "health body was JSON but not an object",
        }

    return {
        "status": body.get("status"),
        "http_code": resp.status_code,
        "checks": body.get("checks") or {},
        "reachable": True,
        "detail": "",
    }


class ValidationThresholds:
    """Plain data so the validator can be constructed in a test."""

    def __init__(
        self,
        max_error_rate: float = 0.05,
        max_p95_seconds: float = 1.5,
        max_cpu_cores: float = 0.9,
        max_memory_bytes: float = 700_000_000.0,
        settle_seconds: int = 20,
        timeout_seconds: int = 180,
        poll_interval_seconds: int = 10,
    ) -> None:
        self.max_error_rate = max_error_rate
        self.max_p95_seconds = max_p95_seconds
        self.max_cpu_cores = max_cpu_cores
        self.max_memory_bytes = max_memory_bytes
        self.settle_seconds = settle_seconds
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds


# ---------------------------------------------------------------------------
# Individual check functions. Pure, so test_validation.py can hammer them
# without a cluster. Each returns (ok, detail).
# ---------------------------------------------------------------------------
def check_replicas(deployment: dict[str, Any] | None) -> tuple[bool, str]:
    if deployment is None:
        return False, "could not read the Deployment"
    desired = deployment.get("desired_replicas")
    available = deployment.get("available_replicas")
    if desired is None:
        return False, "Deployment has no spec.replicas"
    if available == desired:
        return True, f"{available}/{desired} replicas available"
    return False, f"only {available}/{desired} replicas available"


def check_pods_ready(pods: list[dict[str, Any]]) -> tuple[bool, str]:
    """All pods Ready, none in a crash/pull backoff.

    An EMPTY pod list fails. Zero pods is not "all pods healthy" — it is
    either a scaled-to-zero Deployment or a failed label selector, and both
    deserve a human. This is the same "absence is not success" principle as
    chaos verification.
    """
    if not pods:
        return False, "no pods matched the selector"
    not_ready = [p["name"] for p in pods if not p.get("ready")]
    backoff = [
        f"{p['name']}/{c.get('name')}={c.get('waiting_reason')}"
        for p in pods
        for c in (p.get("container_states") or [])
        if c.get("waiting_reason")
        in {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerError"}
    ]
    if backoff:
        return False, "containers in backoff: " + ", ".join(backoff)
    if not_ready:
        return False, "pods not Ready: " + ", ".join(not_ready)
    return True, f"all {len(pods)} pod(s) Ready"


def check_health(health: dict[str, Any]) -> tuple[ValidationOutcome, str]:
    """Interpret a probe_health() result.

    Returns a ValidationOutcome rather than a bool because `degraded` is a
    genuinely third state — the service recovered, its downstream did not —
    and collapsing it into pass or fail would lose the one piece of
    information the JSON body exists to convey.
    """
    if not health.get("reachable"):
        return ValidationOutcome.UNAVAILABLE, health.get("detail", "health unreachable")

    status = health.get("status")
    code = health.get("http_code")

    if status in ("ready", "ok"):
        return ValidationOutcome.PASSED, f"health status={status} (HTTP {code})"
    if status == "degraded":
        return (
            ValidationOutcome.DEGRADED,
            f"health status=degraded with HTTP {code}: the service is serving but a "
            "downstream dependency is unreachable. A status-code-only check would "
            "have reported this as healthy.",
        )
    if status == "not_ready":
        return (
            ValidationOutcome.FAILED,
            f"health status=not_ready (HTTP {code}), checks={health.get('checks')}",
        )
    if status == "unparsed":
        return (
            ValidationOutcome.UNAVAILABLE,
            f"health endpoint returned an unrecognised body (HTTP {code}); "
            "cannot validate from it",
        )
    return ValidationOutcome.FAILED, f"unrecognised health status {status!r} (HTTP {code})"


def check_error_rate(
    current: float | None, threshold: float, baseline: float | None
) -> tuple[bool, str]:
    """5xx rate below threshold, or convincingly decaying towards it.

    `None` fails. A missing error-rate series means the scrape target is gone,
    which during recovery validation is a bad sign rather than a clean bill of
    health.

    See the module docstring on metric lag for why a decaying rate counts as
    recovered.
    """
    if current is None:
        return False, "error rate unavailable (no series returned)"
    if current <= threshold:
        return True, f"error rate {current:.2%} is at or below {threshold:.2%}"
    if (
        baseline is not None
        and baseline > threshold
        and current <= baseline * ERROR_RATE_RECOVERY_FRACTION
    ):
        return True, (
            f"error rate {current:.2%} is still above the {threshold:.2%} threshold "
            f"but has fallen to {current / baseline:.0%} of the {baseline:.2%} "
            "observed before remediation. rate() uses a 5m window, so the bad "
            "minutes are still inside it; this is treated as recovering."
        )
    return False, f"error rate {current:.2%} is above {threshold:.2%}"


def check_latency(current: float | None, threshold: float) -> tuple[bool, str]:
    if current is None:
        # Unlike error rate, an absent latency histogram is common on a
        # low-traffic service that has had no requests since the restart.
        # Treated as a pass with an explicit note rather than blocking
        # resolution on traffic we cannot generate.
        return True, "p95 latency unavailable (likely no requests since the action)"
    if current <= threshold:
        return True, f"p95 latency {current:.2f}s is at or below {threshold:.2f}s"
    return False, f"p95 latency {current:.2f}s is above {threshold:.2f}s"


def check_cpu(current: float | None, threshold: float) -> tuple[bool, str]:
    if current is None:
        return True, "CPU unavailable; not treated as a failure"
    if current <= threshold:
        return True, f"CPU {current:.2f} cores is at or below {threshold:.2f}"
    return False, (
        f"CPU {current:.2f} cores is above {threshold:.2f}. This is "
        "process_cpu_seconds_total, not a cgroup metric — with no cAdvisor here "
        "it cannot be compared to the container's limit."
    )


def check_memory(current: float | None, threshold: float) -> tuple[bool, str]:
    if current is None:
        return True, "memory unavailable; not treated as a failure"
    if current <= threshold:
        return True, f"RSS {current / 1e6:.0f} MB is at or below {threshold / 1e6:.0f} MB"
    return False, f"RSS {current / 1e6:.0f} MB is above {threshold / 1e6:.0f} MB"


# ---------------------------------------------------------------------------
# The polling validator
# ---------------------------------------------------------------------------
class RecoveryValidator:
    def __init__(
        self,
        prom: PrometheusClient,
        k8s: KubernetesClient,
        thresholds: ValidationThresholds,
        base_url_resolver=None,
        sleeper=None,
        clock=None,
    ) -> None:
        self.prom = prom
        self.k8s = k8s
        self.thresholds = thresholds
        self.base_url_resolver = base_url_resolver or (lambda _name: None)
        # Injectable so tests do not actually wait. Production passes
        # asyncio.sleep.
        self._sleep = sleeper or asyncio.sleep
        # The clock is injected together with the sleeper for a specific
        # reason: a fake sleeper that returns immediately does NOT advance
        # wall-clock time, so a timeout loop written against time.time()
        # would spin forever under test. Injecting both lets a test supply a
        # sleeper that advances its own clock, which is the only way to
        # exercise the timeout path at all.
        self._now = clock or time.time

    def is_available_for(self, deployment: str | None) -> bool:
        """Can we validate recovery for this target at all?

        This is the `recovery_validation_available` precondition the Policy
        Engine requires before authorising a rollback. It is true when we have
        both a Kubernetes view and an HTTP health surface — the two
        independent signals. Metrics alone are not enough, because a rollback
        that produces zero traffic would look identical to a rollback that
        fixed everything.
        """
        if not deployment:
            return False
        if not self.k8s.available:
            return False
        return self.base_url_resolver(deployment) is not None

    async def validate(
        self,
        incident: Incident,
        params: ActionParams,
        baseline_error_rate: float | None = None,
    ) -> ValidationReport:
        """Settle, then poll until pass or timeout."""
        namespace = params.namespace or incident.namespace
        deployment = params.deployment or incident.target_deployment
        app = incident.app
        started = self._now()

        if not deployment:
            return ValidationReport(
                outcome=ValidationOutcome.UNAVAILABLE,
                detail="no deployment target to validate",
                elapsed_seconds=0.0,
            )

        # Settle once. Checking availableReplicas immediately after a patch
        # would pass against the pods we are trying to replace.
        await self._sleep(self.thresholds.settle_seconds)

        deadline = started + self.thresholds.timeout_seconds
        last_report: ValidationReport | None = None
        attempt = 0

        while True:
            attempt += 1
            last_report = await self._single_pass(
                namespace, deployment, app, baseline_error_rate
            )
            last_report.elapsed_seconds = self._now() - started
            if last_report.outcome is ValidationOutcome.PASSED:
                logger.info(
                    "validation_passed",
                    extra={
                        "deployment": deployment,
                        "attempt": attempt,
                        "elapsed_seconds": last_report.elapsed_seconds,
                    },
                )
                return last_report

            if self._now() + self.thresholds.poll_interval_seconds >= deadline:
                # Out of time. Preserve the *reason* rather than flattening
                # everything to "timeout": a run that ends DEGRADED is much
                # more informative than one that ends TIMEOUT, and the
                # documentation phase reads this.
                if last_report.outcome is ValidationOutcome.DEGRADED:
                    logger.warning(
                        "validation_ended_degraded",
                        extra={"deployment": deployment, "failed": last_report.failed_checks},
                    )
                    return last_report
                last_report.outcome = ValidationOutcome.TIMEOUT
                last_report.detail = (
                    f"validation did not pass within "
                    f"{self.thresholds.timeout_seconds}s; still failing: "
                    + "; ".join(last_report.failed_checks)
                )
                logger.warning(
                    "validation_timeout",
                    extra={"deployment": deployment, "failed": last_report.failed_checks},
                )
                return last_report

            await self._sleep(self.thresholds.poll_interval_seconds)

    async def _single_pass(
        self,
        namespace: str,
        deployment: str,
        app: str | None,
        baseline_error_rate: float | None,
    ) -> ValidationReport:
        """One full evaluation of every check, all gathered concurrently."""
        base_url = self.base_url_resolver(deployment)

        tasks: dict[str, Any] = {
            "error_rate": self.prom.error_rate(app),
            "p95": self.prom.p95_latency(app),
            "cpu": self.prom.cpu_cores(app),
            "memory": self.prom.memory_bytes(app),
            "chaos": self.prom.chaos_state(app),
        }
        if self.k8s.available:
            tasks["deployment"] = self.k8s.get_deployment(namespace, deployment)
            tasks["pods"] = self.k8s.list_pods(namespace, label_selector=f"app={deployment}")
        if base_url:
            tasks["health"] = probe_health(base_url)

        keys = list(tasks.keys())
        gathered = await asyncio.gather(
            *(tasks[k] for k in keys), return_exceptions=True
        )
        data: dict[str, Any] = {}
        for key, value in zip(keys, gathered, strict=True):
            data[key] = None if isinstance(value, BaseException) else value

        report = ValidationReport(outcome=ValidationOutcome.PASSED)

        def record(name: str, ok: bool, detail: str) -> None:
            report.checks[name] = {"ok": ok, "detail": detail}
            if not ok:
                report.failed_checks.append(f"{name}: {detail}")

        # -- Kubernetes --------------------------------------------------
        if "deployment" in tasks:
            ok, detail = check_replicas(data.get("deployment"))
            record("replicas_available", ok, detail)
            ok, detail = check_pods_ready(data.get("pods") or [])
            record("pods_ready", ok, detail)
        else:
            report.skipped_checks.append(
                "replicas_available/pods_ready: Kubernetes API unavailable"
            )

        # -- health endpoint (JSON status, not status code) ---------------
        health_outcome = ValidationOutcome.UNAVAILABLE
        if "health" in tasks and isinstance(data.get("health"), dict):
            health_outcome, detail = check_health(data["health"])
            record(
                "health_endpoint",
                health_outcome is ValidationOutcome.PASSED,
                detail,
            )
        else:
            report.skipped_checks.append(
                "health_endpoint: no health URL configured for this target"
            )

        # -- metrics -----------------------------------------------------
        ok, detail = check_error_rate(
            data.get("error_rate"), self.thresholds.max_error_rate, baseline_error_rate
        )
        record("error_rate", ok, detail)

        ok, detail = check_latency(data.get("p95"), self.thresholds.max_p95_seconds)
        record("p95_latency", ok, detail)

        ok, detail = check_cpu(data.get("cpu"), self.thresholds.max_cpu_cores)
        record("cpu", ok, detail)

        ok, detail = check_memory(data.get("memory"), self.thresholds.max_memory_bytes)
        record("memory", ok, detail)

        # -- chaos gauges, per pod ---------------------------------------
        # Only asserted when the app is one that exports them. An empty dict
        # from a service with no chaos surface would otherwise fail forever;
        # verify_cleared() treats an empty dict as "cannot confirm", which is
        # correct after a reset but wrong for the frontend.
        chaos_state = data.get("chaos") or {}
        if chaos_state:
            cleared, offenders = verify_cleared(chaos_state)
            record(
                "chaos_cleared",
                cleared,
                "all chaos_* gauges are 0 on every visible pod"
                if cleared
                else "; ".join(offenders),
            )
        else:
            report.skipped_checks.append(
                "chaos_cleared: no chaos_* gauges visible for this app "
                "(expected for the frontend, which has no chaos API)"
            )

        # -- verdict ------------------------------------------------------
        if report.failed_checks:
            # DEGRADED wins over FAILED only when the health endpoint is the
            # *only* problem. If replicas are also missing, the incident is
            # not merely degraded.
            if (
                health_outcome is ValidationOutcome.DEGRADED
                and len(report.failed_checks) == 1
                and report.failed_checks[0].startswith("health_endpoint")
            ):
                report.outcome = ValidationOutcome.DEGRADED
                report.detail = (
                    "everything recovered except the downstream dependency: "
                    "/readyz reports status=degraded. The service itself is "
                    "healthy, so this is a partial recovery, not a failure."
                )
            else:
                report.outcome = ValidationOutcome.FAILED
                report.detail = "; ".join(report.failed_checks)
        else:
            report.detail = "all checks passed"

        return report
