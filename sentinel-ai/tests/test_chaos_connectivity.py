"""Connectivity contract for Sentinel's service-owned chaos APIs."""
from __future__ import annotations

import asyncio

import httpx

from app.clients.chaos_client import ChaosClient
from app.core.config import Settings


def test_in_cluster_configuration_selects_service_dns():
    settings = Settings(
        kubernetes_mode="in_cluster",
        citizen_service_url="http://citizen-service:8000",
        notification_service_url="http://notification-service:8000",
    )

    assert settings.base_url_for("citizen-service") == "http://citizen-service:8000"
    assert settings.base_url_for("notification-service") == "http://notification-service:8000"


def test_remote_configuration_rejects_kubernetes_service_dns():
    settings = Settings(
        kubernetes_mode="remote",
        citizen_service_url="http://citizen-service:8000",
        notification_service_url="http://notification-service.citizen-portal.svc:8000",
    )

    assert settings.base_url_for("citizen-service") is None
    assert settings.base_url_for("notification-service") is None
    assert settings.endpoint_configuration_error(
        "http://citizen-service:8000", remote=True
    )


def test_remote_configuration_selects_private_endpoints():
    settings = Settings(
        kubernetes_mode="remote",
        citizen_service_url="http://10.20.1.10:30080",
        notification_service_url="http://10.20.1.10:30081",
    )

    assert settings.base_url_for("citizen-service") == "http://10.20.1.10:30080"
    assert settings.base_url_for("notification-service") == "http://10.20.1.10:30081"


def test_missing_or_invalid_endpoint_is_unavailable():
    settings = Settings(
        kubernetes_mode="remote",
        citizen_service_url="",
        notification_service_url="not-a-url",
    )

    assert settings.base_url_for("citizen-service") is None
    assert settings.base_url_for("notification-service") is None
    assert settings.endpoint_configuration_error("") == "endpoint is empty"
    assert settings.endpoint_configuration_error("not-a-url")


def test_reset_posts_to_configured_endpoint_and_returns_http_result(monkeypatch):
    requests: list[httpx.Request] = []

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers):
            requests.append(httpx.Request("POST", url, headers=headers))
            return httpx.Response(200, json={"db_failure": False})

    monkeypatch.setattr("app.clients.chaos_client.httpx.AsyncClient", FakeAsyncClient)
    outcome = asyncio.run(
        ChaosClient("token", reset_max_attempts=1).reset("http://10.20.1.10:30080")
    )

    assert outcome.succeeded is True
    assert outcome.attempts == 1
    assert str(requests[0].url) == "http://10.20.1.10:30080/api/chaos/reset"
    assert requests[0].headers["x-chaos-token"] == "token"


def test_reset_retries_transient_dns_failure(monkeypatch):
    attempts = 0

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers):
            nonlocal attempts
            attempts += 1
            raise httpx.ConnectError(
                "[Errno -3] Temporary failure in name resolution",
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("app.clients.chaos_client.httpx.AsyncClient", FakeAsyncClient)
    outcome = asyncio.run(
        ChaosClient("token", reset_max_attempts=3, reset_backoff_seconds=0).reset(
            "http://10.20.1.10:30080"
        )
    )

    assert attempts == 3
    assert outcome.succeeded is False
    assert outcome.transient is True
    assert outcome.attempts == 3