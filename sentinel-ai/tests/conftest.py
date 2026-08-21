"""
Shared fixtures and fakes.

Everything here is designed so the test suite runs with **no network, no
Kubernetes cluster and no API keys**. That is not a convenience — the Policy
Engine and the Remediation Engine allow-list are the security-critical parts
of Sentinel, and a test that only runs in a cluster is a test that never runs.

The fakes deliberately mimic the *shape* of the real client returns (the
trimmed dicts from KubernetesClient, the float|None from
PrometheusClient.scalar) rather than the client objects themselves, because
that shape is the actual contract the lifecycle depends on.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

# Make `app` importable when pytest is run from either sentinel-ai/ or the
# repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.lifecycle.policy import PolicyConfig, PolicyContext  # noqa: E402
from app.models.incident import (  # noqa: E402
    ActionParams,
    ActionPlan,
    Evidence,
    Incident,
    RemediationAction,
    RemediationResult,
    Severity,
)


# ---------------------------------------------------------------------------
# Config / model builders
# ---------------------------------------------------------------------------
@pytest.fixture
def policy_config() -> PolicyConfig:
    """The production defaults, spelled out.

    Written literally rather than derived from `settings` so that a change to
    a default in config.py cannot silently change what these tests assert.
    If someone loosens a threshold, these tests should keep asserting the
    documented value and the mismatch should be a deliberate edit.
    """
    return PolicyConfig(
        allowed_namespaces=frozenset({"citizen-portal"}),
        allowed_deployments=frozenset(
            {"citizen-service", "notification-service", "frontend"}
        ),
        denied_deployments=frozenset({"citizen-postgres", "notification-postgres"}),
        denied_namespaces=frozenset({"kube-system", "kube-public", "kube-node-lease"}),
        confidence_rollback=0.95,
        confidence_restart=0.90,
        confidence_scale=0.90,
        confidence_chaos_reset=0.90,
        min_replicas=1,
        max_replicas=3,
        max_actions_per_incident=3,
        action_cooldown_seconds=120,
        deployment_correlation_window_minutes=30,
    )


@pytest.fixture
def incident() -> Incident:
    return Incident(
        id="INC-TEST-0001",
        fingerprint="abc123",
        alertname="HighHTTPErrorRate",
        severity=Severity.CRITICAL,
        app="citizen-service",
        namespace="citizen-portal",
        evidence=Evidence(),
    )


@pytest.fixture
def rollback_ready_context() -> PolicyContext:
    """A context where every rollback precondition holds.

    Tests then flip exactly one field to prove that field is load-bearing.
    That style matters here: asserting "rollback is denied" is easy, but
    asserting "rollback is denied *because of this specific check*" is what
    proves the check exists rather than being shadowed by another.
    """
    return PolicyContext(
        previous_revision_exists=True,
        deployment_history_count=3,
        last_deploy_age_seconds=300.0,  # 5 minutes
        deploy_correlates_with_onset=True,
        rollback_reversible=True,
        recovery_validation_available=True,
        current_replicas=1,
        chaos_surface_available=True,
        target_is_stateful=False,
    )


def make_plan(
    action: RemediationAction,
    confidence: float = 0.99,
    namespace: str = "citizen-portal",
    deployment: str | None = "citizen-service",
    replicas: int | None = None,
    target_revision: int | None = None,
    service: str | None = None,
) -> ActionPlan:
    return ActionPlan(
        action=action,
        params=ActionParams(
            namespace=namespace,
            deployment=deployment,
            replicas=replicas,
            target_revision=target_revision,
            service=service or deployment,
        ),
        confidence=confidence,
        rationale="test",
    )


def executed_attempt(
    plan: ActionPlan, started_at: float | None = None, succeeded: bool = True
):
    """An AttemptRecord that counts as an executed action.

    Used for the action-cap and cooldown tests. The distinction between an
    attempt with a `result` and one without is exactly what those two policy
    checks key off, so the helper makes it explicit.
    """
    from app.models.incident import AttemptRecord

    return AttemptRecord(
        plan=plan,
        verdict=None,
        result=RemediationResult(
            action=plan.action,
            params=plan.params,
            succeeded=succeeded,
            started_at=started_at if started_at is not None else time.time(),
        ),
    )


# ---------------------------------------------------------------------------
# Fake clients
# ---------------------------------------------------------------------------
class FakePrometheus:
    """Returns canned values. Mirrors PrometheusClient's public surface."""

    def __init__(self, **values):
        self.values = values
        self.calls: list[str] = []

    async def error_rate(self, app, window="5m"):
        self.calls.append("error_rate")
        return self.values.get("error_rate")

    async def error_count(self, app, window="5m"):
        return self.values.get("error_count")

    async def request_rate(self, app, window="5m"):
        return self.values.get("request_rate")

    async def p95_latency(self, app, window="5m"):
        return self.values.get("p95")

    async def cpu_cores(self, app, window="5m"):
        return self.values.get("cpu")

    async def memory_bytes(self, app):
        return self.values.get("memory")

    async def memory_growth_bytes(self, app, window="30m"):
        return self.values.get("memory_growth")

    async def up(self, app):
        return self.values.get("up")

    async def chaos_state(self, app):
        return self.values.get("chaos_state", {})

    async def chaos_injections(self, app, window="10m"):
        return self.values.get("chaos_injections", {})

    async def notification_deliveries(self, window="10m"):
        return self.values.get("notification_deliveries", {})

    async def notification_dispatch_failures(self, window="10m"):
        return self.values.get("notification_dispatch_failures")


class FakeKubernetes:
    """Records every write so tests can assert on what was (not) called."""

    def __init__(self, available=True, deployment=None, pods=None, replicasets=None):
        self.available = available
        self.init_error = "" if available else "fake: not in cluster"
        self._deployment = deployment
        self._pods = pods or []
        self._replicasets = replicasets or []
        self.writes: list[tuple[str, dict]] = []

    async def get_deployment(self, namespace, name):
        return self._deployment

    async def list_pods(self, namespace, label_selector=None):
        return self._pods

    async def list_events(self, namespace, since_seconds=3600.0, limit=100):
        return []

    async def list_replicasets(self, namespace, deployment):
        return self._replicasets

    async def restart_deployment(self, namespace, name):
        self.writes.append(("restart", {"namespace": namespace, "name": name}))
        return {"restarted_at": "2026-01-01T00:00:00Z", "generation": 2}

    async def patch_deployment_template(self, namespace, name, template, target_revision):
        self.writes.append(
            (
                "rollback",
                {"namespace": namespace, "name": name, "revision": target_revision},
            )
        )
        return {"target_revision": target_revision, "generation": 3}

    async def scale_deployment(self, namespace, name, replicas):
        self.writes.append(
            ("scale", {"namespace": namespace, "name": name, "replicas": replicas})
        )
        return {"replicas": replicas, "generation": 4}


class FakeChaos:
    def __init__(self, configured=True, succeed=True):
        self.configured = configured
        self.succeed = succeed
        self.resets: list[str] = []

    async def reset(self, base_url):
        from app.clients.chaos_client import ChaosResetOutcome

        self.resets.append(base_url)
        return ChaosResetOutcome(
            succeeded=self.succeed,
            http_status=200 if self.succeed else 404,
            detail="fake reset",
        )


@pytest.fixture
def fake_k8s():
    return FakeKubernetes()


@pytest.fixture
def fake_chaos():
    return FakeChaos()
