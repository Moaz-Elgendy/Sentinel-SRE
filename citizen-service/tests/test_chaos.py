import pytest
import time

from app.chaos.state import controller
from app.core.config import settings


@pytest.fixture(autouse=True)
def _reset_chaos_state():
    controller.reset()
    yield
    controller.reset()


def test_chaos_control_requires_mode_and_token(client, monkeypatch):
    monkeypatch.setattr(settings, "chaos_mode", True)
    monkeypatch.setattr(settings, "chaos_admin_token", "test-token")

    assert client.get("/api/chaos/status").status_code == 404
    assert client.get("/api/chaos/status", headers={"X-Chaos-Token": "wrong"}).status_code == 404

    response = client.get("/api/chaos/status", headers={"X-Chaos-Token": "test-token"})
    assert response.status_code == 200
    assert response.json()["enabled"] is True


def test_chaos_fault_and_reset(client, monkeypatch):
    monkeypatch.setattr(settings, "chaos_mode", True)
    monkeypatch.setattr(settings, "chaos_admin_token", "test-token")

    response = client.post(
        "/api/chaos/fault",
        headers={"X-Chaos-Token": "test-token"},
        json={"latency_ms": 25, "error_rate": 1.0, "db_failure": False},
    )
    assert response.status_code == 200
    assert response.json()["latency_ms"] == 25
    assert response.json()["error_rate"] == 1.0

    response = client.post("/api/chaos/reset", headers={"X-Chaos-Token": "test-token"})
    assert response.status_code == 200
    assert response.json()["latency_ms"] == 0
    assert response.json()["error_rate"] == 0
    assert response.json()["db_failure"] is False


def test_forced_http_failure_leaves_observability_endpoints_usable(client, monkeypatch):
    monkeypatch.setattr(settings, "chaos_mode", True)
    monkeypatch.setattr(settings, "chaos_admin_token", "test-token")
    controller.update(error_rate=1.0)

    assert client.get("/healthz").status_code == 200
    assert client.get("/metrics").status_code == 200
    assert client.get("/api/services").status_code == 503

    controller.reset()


def test_latency_injection_is_applied(client, monkeypatch):
    monkeypatch.setattr(settings, "chaos_mode", True)
    controller.update(latency_ms=60)

    start = time.perf_counter()
    response = client.get("/api/services")
    elapsed = time.perf_counter() - start

    assert response.status_code == 200
    assert elapsed >= 0.05
    controller.reset()


def test_database_failure_degrades_readiness_and_api(client, monkeypatch):
    monkeypatch.setattr(settings, "chaos_mode", True)
    controller.update(db_failure=True)

    assert client.get("/readyz").status_code == 503
    assert client.get("/readyz").json()["checks"]["database"] == "down"
    assert client.get("/api/services").status_code == 503

    controller.reset()
