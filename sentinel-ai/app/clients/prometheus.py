"""
Prometheus HTTP API client (read-only).

Only two endpoints are used: /api/v1/query and /api/v1/query_range. Sentinel
never writes to Prometheus and never talks to the admin API.

Every metric name and label used here is one that actually exists in this
cluster. Recording the verified facts, because getting these wrong is the
classic way an "AI SRE" ends up confidently querying a series that returns
nothing and then calling the service healthy:

  * `http_requests_total{method,status,handler}` — `status` is a *string*
    like "500"/"503". Chaos-injected failures are 503.
  * `http_request_duration_seconds_bucket{handler,method,le}`
  * `process_cpu_seconds_total`, `process_resident_memory_bytes` — these come
    from prometheus_client inside each Python process. There is **no
    cAdvisor and no kube-state-metrics** in this cluster, so per-container
    CPU/memory from the kubelet is simply not available. Process-level gauges
    are the only CPU/memory signal we have; they miss anything happening
    outside the Python process and they know nothing about container limits.
  * `up{job="kubernetes-pods"}`
  * `chaos_injections_total{fault_type}`, fault_type in
    latency|database|http_5xx|notification_delivery
  * `chaos_latency_ms`, `chaos_error_rate`, `chaos_db_failure` (always
    present, db_failure is 1/0), `chaos_notification_failure_rate`
    (notification-service only)
  * business counters: `citizen_registrations_total`,
    `citizen_logins_total{result}`, `service_requests_total{status}`,
    `notification_dispatches_total{result}` (result=success|failure — this is
    citizen-service's view of *calling* notification-service),
    `notification_deliveries_total{channel,result}` where **result is
    capitalised: `Sent`/`Failed`**.

Common labels on every series: `app`, `kubernetes_pod_name`,
`kubernetes_namespace`.

A note on failure handling: a query that errors returns None / empty rather
than raising. Sentinel must degrade to "I could not see this" and say so in
the evidence bundle, because raising here would mean a Prometheus blip stops
Sentinel from remediating an unrelated outage.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# `status` is a string label on http_requests_total, so 5xx has to be matched
# with a regex. Defined once as a constant rather than inlined, because
# nesting these quotes inside an f-string is exactly how you end up querying
# a matcher that silently matches nothing.
SERVER_ERROR_MATCHER = 'status=~"5.."'
UP_JOB_MATCHER = 'job="kubernetes-pods"'


def escape_label_value(value: str) -> str:
    r"""Escape a value for interpolation into a PromQL label matcher.

    PromQL string literals are Go-style: backslash and the quote character
    need escaping, and newlines are never legal. This is not a general
    sanitiser — the real protection is that every value reaching this
    function comes from an Alertmanager label or our own allow-list, never
    from an LLM.
    """
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", " ")
        .replace("\r", " ")
    )


def selector(
    app: str | None = None,
    namespace: str | None = None,
    extra: list[str] | None = None,
) -> str:
    """Build a `{label="value",...}` matcher, or "" when there is nothing.

    Kept as a module-level function (not a method) purely so the unit tests
    can assert on the generated PromQL without constructing a client.
    """
    parts: list[str] = []
    if app:
        parts.append(f'app="{escape_label_value(app)}"')
    if namespace:
        parts.append(f'kubernetes_namespace="{escape_label_value(namespace)}"')
    parts.extend(extra or [])
    if not parts:
        return ""
    return "{" + ",".join(parts) + "}"


class PrometheusClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ---- raw API --------------------------------------------------------
    async def query(self, promql: str) -> list[dict[str, Any]]:
        """Instant query. Returns the raw `result` array, or [] on any error."""
        return await self._get(
            "/api/v1/query", {"query": promql}, context=f"query={promql}"
        )

    async def query_range(
        self, promql: str, start: float, end: float, step: str = "30s"
    ) -> list[dict[str, Any]]:
        """Range query. Used for "did this get worse over time" questions."""
        return await self._get(
            "/api/v1/query_range",
            {"query": promql, "start": str(start), "end": str(end), "step": step},
            context=f"query_range={promql}",
        )

    async def _get(
        self, path: str, params: dict[str, str], context: str
    ) -> list[dict[str, Any]]:
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, params=params)
            if resp.status_code != 200:
                logger.warning(
                    "prometheus_query_http_error",
                    extra={"status_code": resp.status_code, "query_context": context},
                )
                return []
            payload = resp.json()
            if payload.get("status") != "success":
                logger.warning(
                    "prometheus_query_failed",
                    extra={
                        "error_detail": str(payload.get("error"))[:300],
                        "query_context": context,
                    },
                )
                return []
            return payload.get("data", {}).get("result", []) or []
        except (httpx.HTTPError, ValueError) as exc:
            # ValueError covers a non-JSON body, which is what you get when
            # something other than Prometheus answers on that port.
            logger.warning(
                "prometheus_unreachable",
                extra={"error_detail": str(exc)[:300], "query_context": context},
            )
            return []

    # ---- scalar helpers -------------------------------------------------
    async def scalar(self, promql: str) -> float | None:
        """First sample of an instant query as a float, or None.

        None means "no data" and is NOT the same as 0.0. Treating a missing
        error-rate series as 0 would make Sentinel declare recovery when the
        scrape target has actually disappeared, so every caller handles None
        explicitly.
        """
        result = await self.query(promql)
        if not result:
            return None
        try:
            return float(result[0]["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            return None

    async def series_by_label(self, promql: str, label: str) -> dict[str, float]:
        """Instant query -> {label_value: sample}. Series missing the label
        are skipped rather than bucketed under a fake key."""
        out: dict[str, float] = {}
        for item in await self.query(promql):
            key = (item.get("metric") or {}).get(label)
            if key is None:
                continue
            try:
                out[key] = float(item["value"][1])
            except (KeyError, IndexError, TypeError, ValueError):
                continue
        return out

    # ---- domain queries -------------------------------------------------
    # These build the PromQL so no other module needs to know metric names.
    # If a metric is ever renamed, this section is the only place to change.

    async def error_rate(self, app: str | None, window: str = "5m") -> float | None:
        """Fraction of requests returning 5xx over `window`.

        `status=~"5.."` rather than `status="500"`: chaos injects **503** and
        a genuinely bad deploy usually gives 500. We want both.

        The `or vector(0)` on the numerator is deliberate — with zero 5xx the
        numerator series does not exist at all, the division returns empty,
        and we would read that as "no data" instead of "no errors".
        """
        err_sel = selector(app, extra=[SERVER_ERROR_MATCHER])
        numerator = f"sum(rate(http_requests_total{err_sel}[{window}]))"
        denominator = f"sum(rate(http_requests_total{selector(app)}[{window}]))"
        return await self.scalar(f"({numerator} or vector(0)) / ({denominator})")

    async def error_count(self, app: str | None, window: str = "5m") -> float | None:
        err_sel = selector(app, extra=[SERVER_ERROR_MATCHER])
        return await self.scalar(
            f"sum(increase(http_requests_total{err_sel}[{window}])) or vector(0)"
        )

    async def request_rate(self, app: str | None, window: str = "5m") -> float | None:
        return await self.scalar(
            f"sum(rate(http_requests_total{selector(app)}[{window}]))"
        )

    async def p95_latency(self, app: str | None, window: str = "5m") -> float | None:
        """p95 in seconds from the histogram buckets.

        Aggregating by `le` only (dropping handler/method) gives one number
        for the service, which is the right granularity for a go/no-go
        recovery gate; per-handler p95 belongs in Grafana.

        Honest caveat: chaos latency injection sleeps *before* the handler so
        the injected delay does land in this histogram — but chaos-injected
        503s short-circuit even earlier and never reach the duration
        middleware at all, so a large 503 spike can make p95 look
        artificially *good* (the slow requests were never recorded). Never
        use latency alone to decide recovery.
        """
        return await self.scalar(
            f"histogram_quantile(0.95, sum by (le) "
            f"(rate(http_request_duration_seconds_bucket{selector(app)}[{window}])))"
        )

    async def cpu_cores(self, app: str | None, window: str = "5m") -> float | None:
        """CPU in cores, from prometheus_client's process counter. See the
        module docstring about there being no cAdvisor in this cluster."""
        return await self.scalar(
            f"sum(rate(process_cpu_seconds_total{selector(app)}[{window}]))"
        )

    async def memory_bytes(self, app: str | None) -> float | None:
        return await self.scalar(
            f"max(process_resident_memory_bytes{selector(app)})"
        )

    async def memory_growth_bytes(
        self, app: str | None, window: str = "30m"
    ) -> float | None:
        """RSS delta over `window`. Large and positive => leak-shaped.

        `delta()` is counter-reset-naive, but RSS only "resets" when the
        process restarts, and that discontinuity is exactly what we want to
        see (a big negative delta right after a restart is real information).
        """
        return await self.scalar(
            f"max(delta(process_resident_memory_bytes{selector(app)}[{window}]))"
        )

    async def up(self, app: str | None) -> float | None:
        """min(up) so ONE down pod out of N still reads as down (0)."""
        sel = selector(app, extra=[UP_JOB_MATCHER])
        return await self.scalar(f"min(up{sel})")

    async def chaos_state(self, app: str | None) -> dict[str, dict[str, float]]:
        """Per-pod chaos gauges.

        Keyed by `kubernetes_pod_name` because chaos state is **per-pod
        in-memory** in the app services. A reset sent through the Service VIP
        can land on a different replica than the faulted one; on AWS these
        run at 1 replica to make that deterministic, but we still verify
        per-pod here rather than trusting the HTTP 200 from the reset call.

        `chaos_db_failure` is always exported as 1/0, so an absent series
        means "we could not scrape that pod", not "no fault".
        """
        out: dict[str, dict[str, float]] = {}
        sel = selector(app)
        for metric in (
            "chaos_latency_ms",
            "chaos_error_rate",
            "chaos_db_failure",
            # notification-service only; absent elsewhere, which is fine
            # because we treat an absent series as "not applicable".
            "chaos_notification_failure_rate",
        ):
            for item in await self.query(f"{metric}{sel}"):
                pod = (item.get("metric") or {}).get("kubernetes_pod_name", "unknown")
                try:
                    value = float(item["value"][1])
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
                out.setdefault(pod, {})[metric] = value
        return out

    async def chaos_injections(
        self, app: str | None, window: str = "10m"
    ) -> dict[str, float]:
        """increase(chaos_injections_total) by fault_type over `window`."""
        return await self.series_by_label(
            f"sum by (fault_type) "
            f"(increase(chaos_injections_total{selector(app)}[{window}]))",
            "fault_type",
        )

    async def notification_deliveries(self, window: str = "10m") -> dict[str, float]:
        """increase(notification_deliveries_total) by result.

        Reminder: `result` is **capitalised** on this metric — `Sent` /
        `Failed`. Every other result label in this stack is lowercase. Do not
        "fix" this by lowercasing; the series would silently return nothing.
        """
        return await self.series_by_label(
            f"sum by (result) (increase(notification_deliveries_total[{window}]))",
            "result",
        )

    async def notification_dispatch_failures(self, window: str = "10m") -> float | None:
        """citizen-service's own view of *calling* notification-service.

        Distinct from notification_deliveries_total, which is
        notification-service's view of actually delivering. A gap between the
        two is the signature of a downstream problem, which is why we collect
        both.
        """
        return await self.scalar(
            f'sum(increase(notification_dispatches_total{{result="failure"}}[{window}])) '
            f"or vector(0)"
        )
