"""
Remediation Engine allow-list tests.

The Remediation Engine is the last gate before the Kubernetes API. These
tests assert two things:

1. It will not act without a policy authorisation.
2. It re-checks the target against its OWN allow-list even when handed a
   forged authorisation.

Point 2 is what makes "there is no code path that writes to an unapproved
target" a true statement rather than a hope. Every test that forges a verdict
is simulating a bug (or a compromise) upstream, and asserting that the
Remediation Engine still refuses.

These tests call `asyncio.run` directly rather than relying on
pytest-asyncio's mode configuration, so they pass regardless of how pytest is
configured. Zero network, zero Kubernetes: the fake client records writes
into a list.
"""
from __future__ import annotations

import asyncio

import pytest

from app.lifecycle.remediation import RemediationEngine, RemediationRefused
from app.models.incident import (
    ActionParams,
    PolicyVerdict,
    RemediationAction,
)

from .conftest import FakeChaos, FakeKubernetes, make_plan

ALLOWED_NAMESPACES = frozenset({"citizen-portal"})
ALLOWED_DEPLOYMENTS = frozenset({"citizen-service", "notification-service", "frontend"})
DENIED_DEPLOYMENTS = frozenset({"citizen-postgres", "notification-postgres"})
DENIED_NAMESPACES = frozenset({"kube-system", "kube-public", "kube-node-lease"})

BASE_URLS = {
    "citizen-service": "http://citizen-service:8000",
    "notification-service": "http://notification-service:8000",
    "frontend": "http://frontend:80",
}


def build_engine(k8s=None, chaos=None, dry_run=False) -> RemediationEngine:
    return RemediationEngine(
        k8s=k8s or FakeKubernetes(deployment={"desired_replicas": 1}),
        chaos=chaos or FakeChaos(),
        allowed_namespaces=ALLOWED_NAMESPACES,
        allowed_deployments=ALLOWED_DEPLOYMENTS,
        denied_deployments=DENIED_DEPLOYMENTS,
        denied_namespaces=DENIED_NAMESPACES,
        min_replicas=1,
        max_replicas=3,
        base_url_resolver=BASE_URLS.get,
        dry_run=dry_run,
    )


def allow(action: RemediationAction, adjusted: ActionParams | None = None) -> PolicyVerdict:
    """A forged 'allowed' verdict, used to prove the engine does not trust it."""
    return PolicyVerdict(allowed=True, action=action, adjusted_params=adjusted)


# ---------------------------------------------------------------------------
# It refuses without authorisation
# ---------------------------------------------------------------------------
def test_refuses_when_verdict_denies():
    engine = build_engine()
    plan = make_plan(RemediationAction.RESTART_DEPLOYMENT)
    verdict = PolicyVerdict(allowed=False, action=RemediationAction.RESTART_DEPLOYMENT)
    with pytest.raises(RemediationRefused):
        asyncio.run(engine.execute(plan, verdict))


def test_refuses_when_verdict_is_for_a_different_action():
    """A verdict authorising a restart is not an authorisation to roll back."""
    engine = build_engine()
    plan = make_plan(RemediationAction.ROLLBACK_DEPLOYMENT, target_revision=2)
    verdict = allow(RemediationAction.RESTART_DEPLOYMENT)
    with pytest.raises(RemediationRefused):
        asyncio.run(engine.execute(plan, verdict))


# ---------------------------------------------------------------------------
# It re-checks the allow-list even with a forged authorisation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("database", ["citizen-postgres", "notification-postgres"])
def test_refuses_postgres_despite_a_forged_allow(database):
    k8s = FakeKubernetes(deployment={"desired_replicas": 1})
    engine = build_engine(k8s=k8s)
    plan = make_plan(RemediationAction.RESTART_DEPLOYMENT, deployment=database)
    with pytest.raises(RemediationRefused):
        asyncio.run(engine.execute(plan, allow(RemediationAction.RESTART_DEPLOYMENT)))
    assert k8s.writes == [], "a write reached the cluster for a denied target"


def test_refuses_kube_system_despite_a_forged_allow():
    k8s = FakeKubernetes(deployment={"desired_replicas": 1})
    engine = build_engine(k8s=k8s)
    plan = make_plan(
        RemediationAction.RESTART_DEPLOYMENT, namespace="kube-system", deployment="coredns"
    )
    with pytest.raises(RemediationRefused):
        asyncio.run(engine.execute(plan, allow(RemediationAction.RESTART_DEPLOYMENT)))
    assert k8s.writes == []


def test_refuses_unlisted_deployment_despite_a_forged_allow():
    k8s = FakeKubernetes(deployment={"desired_replicas": 1})
    engine = build_engine(k8s=k8s)
    plan = make_plan(RemediationAction.RESTART_DEPLOYMENT, deployment="grafana")
    with pytest.raises(RemediationRefused):
        asyncio.run(engine.execute(plan, allow(RemediationAction.RESTART_DEPLOYMENT)))
    assert k8s.writes == []


def test_refuses_when_adjusted_params_redirect_to_a_denied_target():
    """The engine validates the params it will actually USE.

    A verdict that says 'allowed' for citizen-service but carries
    adjusted_params pointing at citizen-postgres must not be honoured. This is
    the gap that re-checking closes: without it, the verdict alone would
    authorise whatever the params happened to say.
    """
    k8s = FakeKubernetes(deployment={"desired_replicas": 1})
    engine = build_engine(k8s=k8s)
    plan = make_plan(RemediationAction.SCALE_DEPLOYMENT, replicas=2)
    verdict = allow(
        RemediationAction.SCALE_DEPLOYMENT,
        adjusted=ActionParams(
            namespace="citizen-portal", deployment="citizen-postgres", replicas=2
        ),
    )
    with pytest.raises(RemediationRefused):
        asyncio.run(engine.execute(plan, verdict))
    assert k8s.writes == []


def test_assert_target_permitted_is_callable_standalone():
    """The gate is a public method, so a future call site can use it directly."""
    engine = build_engine()
    engine.assert_target_permitted(
        ActionParams(namespace="citizen-portal", deployment="citizen-service")
    )
    with pytest.raises(RemediationRefused):
        engine.assert_target_permitted(
            ActionParams(namespace="citizen-portal", deployment="citizen-postgres")
        )
    with pytest.raises(RemediationRefused):
        engine.assert_target_permitted(ActionParams(namespace=None, deployment=None))


# ---------------------------------------------------------------------------
# Scale band re-asserted locally
# ---------------------------------------------------------------------------
def test_scale_is_clamped_inside_the_engine():
    """Even if policy were bypassed, the engine clamps to the band."""
    k8s = FakeKubernetes(deployment={"desired_replicas": 1})
    engine = build_engine(k8s=k8s)
    plan = make_plan(RemediationAction.SCALE_DEPLOYMENT, replicas=99)
    result = asyncio.run(engine.execute(plan, allow(RemediationAction.SCALE_DEPLOYMENT)))
    assert result.succeeded is True
    assert k8s.writes == [
        ("scale", {"namespace": "citizen-portal", "name": "citizen-service", "replicas": 3})
    ]


def test_scale_to_zero_is_refused_by_the_engine():
    """Belt and braces: min_replicas is 1, so max(1, min(3, 0)) == 1.

    The engine can therefore never write 0. We assert the write that actually
    happened is 1, not 0 — the clamp is what makes scale-to-zero
    unreachable rather than merely disallowed.
    """
    k8s = FakeKubernetes(deployment={"desired_replicas": 2})
    engine = build_engine(k8s=k8s)
    plan = make_plan(RemediationAction.SCALE_DEPLOYMENT, replicas=0)
    result = asyncio.run(engine.execute(plan, allow(RemediationAction.SCALE_DEPLOYMENT)))
    assert result.succeeded is True
    assert k8s.writes[0][1]["replicas"] == 1


# ---------------------------------------------------------------------------
# The happy paths, and dry run
# ---------------------------------------------------------------------------
def test_restart_patches_the_deployment():
    k8s = FakeKubernetes(deployment={"desired_replicas": 1})
    engine = build_engine(k8s=k8s)
    plan = make_plan(RemediationAction.RESTART_DEPLOYMENT)
    result = asyncio.run(engine.execute(plan, allow(RemediationAction.RESTART_DEPLOYMENT)))
    assert result.succeeded is True
    assert k8s.writes[0][0] == "restart"
    assert "restartedAt" in result.detail


def test_rollback_uses_the_previous_revision():
    replicasets = [
        {"name": "cs-3", "revision": 3, "images": ["app:bad"], "_template": {"metadata": {}}},
        {"name": "cs-2", "revision": 2, "images": ["app:good"], "_template": {"metadata": {}}},
    ]
    k8s = FakeKubernetes(deployment={"desired_replicas": 1}, replicasets=replicasets)
    engine = build_engine(k8s=k8s)
    plan = make_plan(RemediationAction.ROLLBACK_DEPLOYMENT, target_revision=2)
    result = asyncio.run(engine.execute(plan, allow(RemediationAction.ROLLBACK_DEPLOYMENT)))
    assert result.succeeded is True
    assert k8s.writes[0] == (
        "rollback",
        {"namespace": "citizen-portal", "name": "citizen-service", "revision": 2},
    )


def test_rollback_fails_cleanly_when_the_revision_vanished():
    """revisionHistoryLimit can prune a revision between investigation and
    execution. That must be a failed attempt, not an exception."""
    k8s = FakeKubernetes(deployment={"desired_replicas": 1}, replicasets=[])
    engine = build_engine(k8s=k8s)
    plan = make_plan(RemediationAction.ROLLBACK_DEPLOYMENT, target_revision=2)
    result = asyncio.run(engine.execute(plan, allow(RemediationAction.ROLLBACK_DEPLOYMENT)))
    assert result.succeeded is False
    assert k8s.writes == []


def test_chaos_reset_posts_to_the_resolved_url_only():
    chaos = FakeChaos()
    engine = build_engine(chaos=chaos)
    plan = make_plan(RemediationAction.RESET_CHAOS_FAULT, service="citizen-service")
    result = asyncio.run(engine.execute(plan, allow(RemediationAction.RESET_CHAOS_FAULT)))
    assert result.succeeded is True
    assert chaos.resets == ["http://citizen-service:8000"]


def test_chaos_reset_will_not_invent_a_url():
    """A target with no configured base URL gets no outbound request.

    This is why the resolver is injected: there is no code path that builds an
    arbitrary URL from a name.
    """
    chaos = FakeChaos()
    engine = build_engine(chaos=chaos)
    plan = make_plan(
        RemediationAction.RESET_CHAOS_FAULT, deployment="frontend", service="unknown-svc"
    )
    result = asyncio.run(engine.execute(plan, allow(RemediationAction.RESET_CHAOS_FAULT)))
    assert result.succeeded is False
    assert chaos.resets == []


def test_dry_run_authorises_but_writes_nothing():
    k8s = FakeKubernetes(deployment={"desired_replicas": 1})
    engine = build_engine(k8s=k8s, dry_run=True)
    plan = make_plan(RemediationAction.RESTART_DEPLOYMENT)
    result = asyncio.run(engine.execute(plan, allow(RemediationAction.RESTART_DEPLOYMENT)))
    assert result.succeeded is True
    assert result.dry_run is True
    assert k8s.writes == []


def test_dry_run_still_enforces_the_allow_list():
    """DRY_RUN is not a bypass for the allow-list."""
    engine = build_engine(dry_run=True)
    plan = make_plan(RemediationAction.RESTART_DEPLOYMENT, deployment="citizen-postgres")
    with pytest.raises(RemediationRefused):
        asyncio.run(engine.execute(plan, allow(RemediationAction.RESTART_DEPLOYMENT)))


def test_no_shell_execution_anywhere_in_the_engine():
    """Structural assertion: the Remediation Engine module imports nothing
    that can execute a command.

    Cheap, but it is the guarantee the whole design rests on, and it would
    catch a future refactor that reached for subprocess 'just to run kubectl'.
    """
    import app.lifecycle.remediation as module

    with open(module.__file__, encoding="utf-8") as handle:
        source = handle.read()
    # Matching call/import syntax rather than the bare words, because the
    # module's own docstring says "No subprocess. No os.system." and we do not
    # want the documentation to fail its own test.
    for forbidden in (
        "import subprocess",
        "os.system(",
        "os.popen(",
        "os.execv",
        "eval(",
        "exec(",
        "__import__(",
        "pty.spawn",
    ):
        assert forbidden not in source, f"{forbidden} appeared in remediation.py"
