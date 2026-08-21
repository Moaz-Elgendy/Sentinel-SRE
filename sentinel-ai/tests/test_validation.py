"""
Recovery validation tests.

The two things most worth protecting here:

1. **Health checks must parse the JSON `status` field.** `/readyz` returns
   HTTP 200 with `{"status":"degraded"}` when notification-service is
   unreachable, so a status-code-only check reports a half-broken service as
   fully healthy. The `degraded` test below is the regression test for that
   entire class of bug.

2. **Absence of data must never read as success.** No pods, no error-rate
   series, no chaos gauges — none of those are evidence of recovery.

Also covered: the polling loop terminates, uses the injected sleeper (so the
suite runs instantly), and preserves DEGRADED rather than flattening it to
TIMEOUT.
"""
from __future__ import annotations

import asyncio

import pytest

from app.clients.chaos_client import verify_cleared
from app.lifecycle.validation import (
    RecoveryValidator,
    ValidationThresholds,
    check_cpu,
    check_error_rate,
    check_health,
    check_latency,
    check_memory,
    check_pods_ready,
    check_replicas,
)
from app.models.incident import ActionParams, ValidationOutcome

from .conftest import FakeKubernetes, FakePrometheus

BASE_URLS = {"citizen-service": "http://citizen-service:8000"}


# ---------------------------------------------------------------------------
# Health status parsing — the important one
# ---------------------------------------------------------------------------
def test_degraded_is_not_treated_as_recovered():
    """HTTP 200 + status=degraded is a downstream outage, not health."""
    outcome, detail = check_health(
        {"reachable": True, "status": "degraded", "http_code": 200, "checks": {}}
    )
    assert outcome is ValidationOutcome.DEGRADED
    assert "downstream" in detail


def test_degraded_is_distinguished_from_not_ready():
    degraded, _ = check_health(
        {"reachable": True, "status": "degraded", "http_code": 200, "checks": {}}
    )
    not_ready, _ = check_health(
        {
            "reachable": True,
            "status": "not_ready",
            "http_code": 503,
            "checks": {"database": "down"},
        }
    )
    assert degraded is ValidationOutcome.DEGRADED
    assert not_ready is ValidationOutcome.FAILED
    assert degraded is not not_ready


def test_ready_passes():
    outcome, _ = check_health(
        {"reachable": True, "status": "ready", "http_code": 200, "checks": {}}
    )
    assert outcome is ValidationOutcome.PASSED


def test_frontend_plain_text_ok_passes():
    """The frontend's /healthz returns the literal text `ok`, not JSON."""
    outcome, _ = check_health(
        {"reachable": True, "status": "ok", "http_code": 200, "checks": {}}
    )
    assert outcome is ValidationOutcome.PASSED


def test_unreachable_health_is_unavailable_not_passed():
    outcome, _ = check_health({"reachable": False, "detail": "connection refused"})
    assert outcome is ValidationOutcome.UNAVAILABLE


# ---------------------------------------------------------------------------
# Absence is not success
# ---------------------------------------------------------------------------
def test_no_pods_fails():
    ok, detail = check_pods_ready([])
    assert ok is False
    assert "no pods" in detail


def test_missing_error_rate_series_fails():
    """A vanished scrape target during validation is a bad sign, not a good one."""
    ok, _ = check_error_rate(None, threshold=0.05, baseline=None)
    assert ok is False


def test_empty_chaos_state_is_not_confirmation():
    cleared, offenders = verify_cleared({})
    assert cleared is False
    assert offenders


def test_missing_deployment_fails_replica_check():
    ok, _ = check_replicas(None)
    assert ok is False


# ---------------------------------------------------------------------------
# Individual metric checks
# ---------------------------------------------------------------------------
def test_replicas_must_match_desired():
    assert check_replicas({"desired_replicas": 2, "available_replicas": 2})[0] is True
    assert check_replicas({"desired_replicas": 2, "available_replicas": 1})[0] is False


def test_crashloop_containers_fail_readiness():
    pods = [
        {
            "name": "cs-1",
            "ready": True,
            "container_states": [{"name": "app", "waiting_reason": "CrashLoopBackOff"}],
        }
    ]
    ok, detail = check_pods_ready(pods)
    assert ok is False
    assert "CrashLoopBackOff" in detail


def test_error_rate_below_threshold_passes():
    assert check_error_rate(0.01, 0.05, None)[0] is True


def test_error_rate_above_threshold_fails():
    assert check_error_rate(0.30, 0.05, None)[0] is False


def test_decaying_error_rate_counts_as_recovering():
    """rate() uses a 5m window, so a fixed spike decays rather than snapping
    to zero. Without this allowance every successful remediation of an error
    spike would report a validation timeout."""
    ok, detail = check_error_rate(current=0.08, threshold=0.05, baseline=0.50)
    assert ok is True
    assert "recovering" in detail


def test_barely_decayed_error_rate_still_fails():
    ok, _ = check_error_rate(current=0.40, threshold=0.05, baseline=0.50)
    assert ok is False


def test_absent_latency_passes_with_a_note():
    """A restarted low-traffic service has no histogram samples yet. Blocking
    resolution on traffic we cannot generate would be wrong."""
    ok, detail = check_latency(None, 1.5)
    assert ok is True
    assert "unavailable" in detail


def test_latency_thresholds():
    assert check_latency(0.5, 1.5)[0] is True
    assert check_latency(2.5, 1.5)[0] is False


def test_cpu_and_memory_thresholds():
    assert check_cpu(0.2, 0.9)[0] is True
    assert check_cpu(1.5, 0.9)[0] is False
    assert check_memory(100e6, 700e6)[0] is True
    assert check_memory(900e6, 700e6)[0] is False


# ---------------------------------------------------------------------------
# Chaos gauge verification, per pod
# ---------------------------------------------------------------------------
def test_chaos_cleared_when_all_gauges_zero():
    cleared, offenders = verify_cleared(
        {"cs-abc": {"chaos_db_failure": 0.0, "chaos_error_rate": 0.0, "chaos_latency_ms": 0.0}}
    )
    assert cleared is True
    assert offenders == []


def test_chaos_not_cleared_when_one_pod_still_faulted():
    """Per-pod verification: a reset through the Service VIP can hit the
    wrong replica, so one dirty pod must fail the whole check."""
    cleared, offenders = verify_cleared(
        {
            "cs-abc": {"chaos_db_failure": 0.0},
            "cs-def": {"chaos_db_failure": 1.0},
        }
    )
    assert cleared is False
    assert any("cs-def" in o for o in offenders)


def test_notification_chaos_gauge_is_checked_when_present():
    cleared, offenders = verify_cleared(
        {"ns-1": {"chaos_notification_failure_rate": 0.5}}
    )
    assert cleared is False
    assert any("notification" in o for o in offenders)


# ---------------------------------------------------------------------------
# The polling loop
# ---------------------------------------------------------------------------
def build_validator(prom_values, k8s, sleeps: list[float]):
    """A validator whose fake sleeper ADVANCES a fake clock.

    This is why the clock is injectable. A sleeper that returns immediately
    without advancing time would make the timeout loop spin forever, so the
    timeout path would be untestable — and an untested timeout in an
    autonomous remediator is a hang during an incident.
    """
    fake_time = {"now": 1_000_000.0}

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        fake_time["now"] += seconds

    return RecoveryValidator(
        prom=FakePrometheus(**prom_values),
        k8s=k8s,
        thresholds=ValidationThresholds(
            settle_seconds=5, timeout_seconds=30, poll_interval_seconds=10
        ),
        base_url_resolver=BASE_URLS.get,
        sleeper=fake_sleep,
        clock=lambda: fake_time["now"],
    )


HEALTHY_POD = {"name": "cs-1", "ready": True, "container_states": []}


def test_validation_passes_when_everything_is_healthy(incident, monkeypatch):
    k8s = FakeKubernetes(
        deployment={"desired_replicas": 1, "available_replicas": 1},
        pods=[HEALTHY_POD],
    )
    sleeps: list[float] = []
    validator = build_validator(
        {"error_rate": 0.0, "p95": 0.1, "cpu": 0.1, "memory": 1e8, "chaos_state": {}},
        k8s,
        sleeps,
    )

    async def fake_probe(base_url, timeout=5.0, path="/readyz"):
        return {"reachable": True, "status": "ready", "http_code": 200, "checks": {}}

    monkeypatch.setattr("app.lifecycle.validation.probe_health", fake_probe)

    report = asyncio.run(
        validator.validate(incident, ActionParams(
            namespace="citizen-portal", deployment="citizen-service"
        ))
    )
    assert report.outcome is ValidationOutcome.PASSED
    # Settled exactly once and did not poll again, because the first pass
    # already succeeded.
    assert sleeps == [5]


def test_validation_times_out_and_reports_why(incident, monkeypatch):
    k8s = FakeKubernetes(
        deployment={"desired_replicas": 2, "available_replicas": 1},
        pods=[HEALTHY_POD],
    )
    sleeps: list[float] = []
    validator = build_validator(
        {"error_rate": 0.9, "p95": 5.0, "cpu": 0.1, "memory": 1e8, "chaos_state": {}},
        k8s,
        sleeps,
    )

    async def fake_probe(base_url, timeout=5.0, path="/readyz"):
        return {"reachable": True, "status": "not_ready", "http_code": 503, "checks": {}}

    monkeypatch.setattr("app.lifecycle.validation.probe_health", fake_probe)

    report = asyncio.run(
        validator.validate(incident, ActionParams(
            namespace="citizen-portal", deployment="citizen-service"
        ))
    )
    assert report.outcome is ValidationOutcome.TIMEOUT
    assert report.failed_checks
    assert any("replicas_available" in c for c in report.failed_checks)
    # Settle (5) then poll at 10s intervals until the 30s deadline: it must
    # terminate, and it must not poll forever.
    assert sleeps == [5, 10, 10]


def test_degraded_is_preserved_and_not_flattened_to_failed(incident, monkeypatch):
    """When the health endpoint is the ONLY failing check, the outcome must be
    DEGRADED — that is the difference between 'a downstream is broken' and
    'the remediation did not work'."""
    k8s = FakeKubernetes(
        deployment={"desired_replicas": 1, "available_replicas": 1},
        pods=[HEALTHY_POD],
    )
    sleeps: list[float] = []
    validator = build_validator(
        {"error_rate": 0.0, "p95": 0.1, "cpu": 0.1, "memory": 1e8, "chaos_state": {}},
        k8s,
        sleeps,
    )

    async def fake_probe(base_url, timeout=5.0, path="/readyz"):
        return {"reachable": True, "status": "degraded", "http_code": 200, "checks": {}}

    monkeypatch.setattr("app.lifecycle.validation.probe_health", fake_probe)

    report = asyncio.run(
        validator.validate(incident, ActionParams(
            namespace="citizen-portal", deployment="citizen-service"
        ))
    )
    assert report.outcome is ValidationOutcome.DEGRADED


def test_validation_unavailable_without_a_target(incident):
    """No deployment to validate => UNAVAILABLE, and no sleeping at all."""
    incident.app = None
    sleeps: list[float] = []
    validator = build_validator({}, FakeKubernetes(), sleeps)
    report = asyncio.run(validator.validate(incident, ActionParams()))
    assert report.outcome is ValidationOutcome.UNAVAILABLE
    assert sleeps == []


def test_is_available_for_requires_kubernetes_and_a_health_url():
    """This is the `recovery_validation_available` rollback precondition."""
    with_k8s = RecoveryValidator(
        prom=FakePrometheus(),
        k8s=FakeKubernetes(available=True),
        thresholds=ValidationThresholds(),
        base_url_resolver=BASE_URLS.get,
    )
    assert with_k8s.is_available_for("citizen-service") is True
    assert with_k8s.is_available_for("some-other-service") is False
    assert with_k8s.is_available_for(None) is False

    without_k8s = RecoveryValidator(
        prom=FakePrometheus(),
        k8s=FakeKubernetes(available=False),
        thresholds=ValidationThresholds(),
        base_url_resolver=BASE_URLS.get,
    )
    assert without_k8s.is_available_for("citizen-service") is False
