"""
Sentinel's own Prometheus metrics.

These are *Sentinel observing itself*. The whole point of an autonomous
remediator is that you can audit it after the fact, and the cheapest audit
trail is a time series: how many incidents, how many actions, how many were
denied by policy and why, how many escalated.

Deliberate cardinality choices:
  * `root_cause` uses the RootCause enum, not free text, so an LLM narrative
    can never blow up the label cardinality.
  * `reason` on denials and escalations uses the DenialReason /
    EscalationReason enums for the same reason.
  * There is no `incident_id` label anywhere. Per-incident detail belongs in
    SQLite and in the /api/incidents API, not in Prometheus.

Note: unlike citizen-service we do NOT use prometheus-fastapi-instrumentator.
Sentinel's inbound HTTP surface is one webhook and two read endpoints; the
per-handler request metrics would be noise, and we want /metrics to contain
only the sentinel_* series so a Grafana panel can wildcard `sentinel_.*`
safely.
"""
from prometheus_client import Counter, Gauge, Histogram

sentinel_incidents_total = Counter(
    "sentinel_incidents_total",
    "Incidents opened by Sentinel, by severity and final/current root cause.",
    ["severity", "root_cause"],
)

sentinel_remediations_total = Counter(
    "sentinel_remediations_total",
    "Remediation actions executed, by action and result (success|failure|dry_run).",
    ["action", "result"],
)

sentinel_remediation_duration_seconds = Histogram(
    "sentinel_remediation_duration_seconds",
    "Wall-clock duration of a remediation action's execution (not validation).",
    ["action"],
    # Buckets tuned for K8s control-plane writes: a deployment patch is
    # normally tens of milliseconds; anything over ~5s means the API server
    # is unhappy, which is itself worth alerting on.
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

sentinel_validation_result_total = Counter(
    "sentinel_validation_result_total",
    "Recovery validation outcomes (passed|failed|timeout|degraded|unavailable).",
    ["result"],
)

sentinel_escalations_total = Counter(
    "sentinel_escalations_total",
    "Incidents handed to a human, by reason.",
    ["reason"],
)

sentinel_llm_calls_total = Counter(
    "sentinel_llm_calls_total",
    "OpenAI enrichment calls (success|error|skipped|rejected).",
    ["result"],
)

sentinel_open_incidents = Gauge(
    "sentinel_open_incidents",
    "Incidents currently not in a terminal state (RESOLVED/ESCALATED/CLOSED).",
)

sentinel_policy_denials_total = Counter(
    "sentinel_policy_denials_total",
    "Candidate actions blocked by the Policy Engine, by action and reason.",
    ["action", "reason"],
)


def observe_remediation(action: str, result: str, duration_seconds: float) -> None:
    """Small helper so callers cannot forget one half of the pair."""
    sentinel_remediations_total.labels(action=action, result=result).inc()
    sentinel_remediation_duration_seconds.labels(action=action).observe(duration_seconds)
