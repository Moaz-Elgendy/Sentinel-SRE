import pytest
import time

from prometheus_client import REGISTRY

from app.chaos.state import _cpu_burner, _leaked_mb, controller
from app.core.config import settings


@pytest.fixture(autouse=True)
def _reset_chaos_state():
    controller.reset()
    yield
    controller.reset()


def test_chaos_control_requires_token(client, monkeypatch):
    monkeypatch.setattr(settings, "chaos_mode", True)
    monkeypatch.setattr(settings, "chaos_admin_token", "test-token")

    assert client.get("/api/chaos/status").status_code == 404
    response = client.get("/api/chaos/status", headers={"X-Chaos-Token": "test-token"})
    assert response.status_code == 200
    assert response.json()["enabled"] is True


def test_chaos_can_configure_all_faults(client, monkeypatch):
    monkeypatch.setattr(settings, "chaos_mode", True)
    monkeypatch.setattr(settings, "chaos_admin_token", "test-token")

    response = client.post(
        "/api/chaos/fault",
        headers={"X-Chaos-Token": "test-token"},
        json={
            "latency_ms": 10,
            "error_rate": 0.5,
            "db_failure": True,
            "notification_failure_rate": 1.0,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["latency_ms"] == 10
    assert body["error_rate"] == 0.5
    assert body["db_failure"] is True
    assert body["notification_failure_rate"] == 1.0

    controller.reset()
    assert controller.get().notification_failure_rate == settings.chaos_failure_rate


def test_forced_http_failure_keeps_health_and_metrics_available(client, monkeypatch):
    monkeypatch.setattr(settings, "chaos_mode", True)
    controller.update(error_rate=1.0)

    assert client.get("/healthz").status_code == 200
    assert client.get("/metrics").status_code == 200
    assert client.get("/api/notifications").status_code == 503

    controller.reset()


def test_latency_injection(client, monkeypatch):
    monkeypatch.setattr(settings, "chaos_mode", True)
    controller.update(latency_ms=60)

    start = time.perf_counter()
    response = client.get("/api/notifications")
    elapsed = time.perf_counter() - start

    assert response.status_code == 200
    assert elapsed >= 0.05
    controller.reset()


def test_database_failure_degrades_readiness(client, monkeypatch):
    monkeypatch.setattr(settings, "chaos_mode", True)
    controller.update(db_failure=True)

    assert client.get("/readyz").status_code == 503
    assert client.get("/readyz").json()["checks"]["database"] == "down"
    assert client.get("/api/notifications").status_code == 503

    controller.reset()


# --- CPU burn / memory leak (resource-exhaustion faults) -------------------
# These two faults differ from the others in that they have real side effects
# outside the state dataclass (a running thread, a retained buffer), so every
# test below asserts the *effect* was applied or reverted, not just that the
# API echoed the value back.

_TEST_LEAK_MB = 16
"""Small on purpose. The point of these tests is the plumbing — bounds,
PATCH semantics, release-on-reset — not the size of the leak, and the test
suite runs in CI containers that should not have to find 2 GiB."""


def test_new_faults_preserve_patch_semantics(client, monkeypatch):
    monkeypatch.setattr(settings, "chaos_mode", True)
    monkeypatch.setattr(settings, "chaos_admin_token", "test-token")
    headers = {"X-Chaos-Token": "test-token"}

    client.post(
        "/api/chaos/fault", headers=headers, json={"latency_ms": 42, "notification_failure_rate": 0.25}
    )

    # Setting only cpu_burn must not disturb any other field.
    body = client.post("/api/chaos/fault", headers=headers, json={"cpu_burn": True}).json()
    assert body["cpu_burn"] is True
    assert body["latency_ms"] == 42
    assert body["notification_failure_rate"] == 0.25
    assert body["memory_leak_mb"] == 0

    # ...and setting only memory_leak_mb must not clear cpu_burn.
    body = client.post(
        "/api/chaos/fault", headers=headers, json={"memory_leak_mb": _TEST_LEAK_MB}
    ).json()
    assert body["memory_leak_mb"] == _TEST_LEAK_MB
    assert body["cpu_burn"] is True
    assert body["latency_ms"] == 42
    assert body["notification_failure_rate"] == 0.25

    controller.reset()


def test_new_faults_reject_out_of_range_values(client, monkeypatch):
    monkeypatch.setattr(settings, "chaos_mode", True)
    monkeypatch.setattr(settings, "chaos_admin_token", "test-token")
    headers = {"X-Chaos-Token": "test-token"}

    assert client.post(
        "/api/chaos/fault", headers=headers, json={"memory_leak_mb": -1}
    ).status_code == 422
    # 2048 MiB is the ceiling: above it we are not simulating a leak, we are
    # just trying to exhaust the node.
    assert client.post(
        "/api/chaos/fault", headers=headers, json={"memory_leak_mb": 2049}
    ).status_code == 422
    assert client.post(
        "/api/chaos/fault", headers=headers, json={"cpu_burn": "maybe"}
    ).status_code == 422


def test_new_faults_still_require_the_chaos_token(client, monkeypatch):
    monkeypatch.setattr(settings, "chaos_mode", True)
    monkeypatch.setattr(settings, "chaos_admin_token", "test-token")

    # 404, not 401/403 — the control plane conceals its own existence.
    assert client.post("/api/chaos/fault", json={"cpu_burn": True}).status_code == 404
    assert client.post(
        "/api/chaos/fault",
        headers={"X-Chaos-Token": "wrong"},
        json={"memory_leak_mb": _TEST_LEAK_MB},
    ).status_code == 404
    assert controller.get().cpu_burn is False
    assert controller.get().memory_leak_mb == 0


def test_reset_stops_cpu_burn_frees_memory_and_keeps_configured_failure_rate(
    client, monkeypatch
):
    monkeypatch.setattr(settings, "chaos_mode", True)
    monkeypatch.setattr(settings, "chaos_admin_token", "test-token")
    headers = {"X-Chaos-Token": "test-token"}

    client.post(
        "/api/chaos/fault",
        headers=headers,
        json={"cpu_burn": True, "memory_leak_mb": _TEST_LEAK_MB},
    )
    assert _cpu_burner.is_running() is True
    assert _leaked_mb() == _TEST_LEAK_MB

    body = client.post("/api/chaos/reset", headers=headers).json()
    assert body["cpu_burn"] is False
    assert body["memory_leak_mb"] == 0
    # The burner thread survives (it parks on an Event) but must no longer be
    # burning, and every chunk reference must be gone.
    assert _cpu_burner.is_running() is False
    assert _leaked_mb() == 0
    # Reset still restores this service's *configured* baseline failure rate
    # rather than zero — unchanged behaviour, asserted here so the new
    # side-effect handling in reset() cannot quietly regress it.
    assert controller.get().notification_failure_rate == settings.chaos_failure_rate


def test_memory_leak_can_be_shrunk_without_a_full_reset(client, monkeypatch):
    monkeypatch.setattr(settings, "chaos_mode", True)
    monkeypatch.setattr(settings, "chaos_admin_token", "test-token")
    headers = {"X-Chaos-Token": "test-token"}

    client.post("/api/chaos/fault", headers=headers, json={"memory_leak_mb": 24})
    assert _leaked_mb() == 24

    client.post("/api/chaos/fault", headers=headers, json={"memory_leak_mb": 8})
    assert _leaked_mb() == 8

    controller.reset()
    assert _leaked_mb() == 0


def test_new_fault_gauges_track_state():
    controller.update(cpu_burn=True, memory_leak_mb=_TEST_LEAK_MB)
    assert REGISTRY.get_sample_value("chaos_cpu_burn") == 1
    assert REGISTRY.get_sample_value("chaos_memory_leak_mb") == _TEST_LEAK_MB

    controller.reset()
    assert REGISTRY.get_sample_value("chaos_cpu_burn") == 0
    assert REGISTRY.get_sample_value("chaos_memory_leak_mb") == 0


def test_cpu_burn_and_memory_leak_are_counted_as_injections():
    def injections(fault_type):
        return REGISTRY.get_sample_value(
            "chaos_injections_total", {"fault_type": fault_type}
        ) or 0.0

    cpu_before = injections("cpu_burn")
    mem_before = injections("memory_leak")

    controller.update(cpu_burn=True, memory_leak_mb=_TEST_LEAK_MB)
    assert injections("cpu_burn") == cpu_before + 1
    assert injections("memory_leak") == mem_before + 1

    # Re-asserting the same values is a no-op, not a second injection.
    controller.update(cpu_burn=True, memory_leak_mb=_TEST_LEAK_MB)
    assert injections("cpu_burn") == cpu_before + 1
    assert injections("memory_leak") == mem_before + 1

    controller.reset()
    # Remediation is not an injection either.
    assert injections("cpu_burn") == cpu_before + 1
    assert injections("memory_leak") == mem_before + 1


def test_health_and_metrics_stay_available_while_cpu_burning(client, monkeypatch):
    # The whole reason the burner runs on a duty cycle: an autonomous agent
    # cannot detect or remediate a CPU incident if the pod stops answering
    # its probes. If this test ever starts failing or timing out, the duty
    # cycle in app/chaos/state.py is too aggressive.
    monkeypatch.setattr(settings, "chaos_mode", True)
    controller.update(cpu_burn=True)
    assert _cpu_burner.is_running() is True

    start = time.perf_counter()
    assert client.get("/healthz").status_code == 200
    assert client.get("/metrics").status_code == 200
    assert client.get("/api/notifications").status_code == 200
    elapsed = time.perf_counter() - start

    # Generous ceiling: this asserts "not wedged", not a latency SLO, so it
    # stays meaningful on a slow/loaded CI machine.
    assert elapsed < 5.0

    controller.reset()
