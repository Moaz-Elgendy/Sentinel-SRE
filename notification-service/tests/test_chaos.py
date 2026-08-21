import pytest
import time

from app.chaos.state import controller
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
