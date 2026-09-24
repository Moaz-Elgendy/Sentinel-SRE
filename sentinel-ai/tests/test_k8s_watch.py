"""
Regression tests for the generic Kubernetes desired-vs-actual watch
(`app/lifecycle/k8s_watch.py`) — the sensor that lets Sentinel notice an
unhealthy Deployment even when no Alertmanager alert was ever going to fire
for it (see that module's docstring for why: `up{...} == 0` cannot fire on a
scrape target that never existed).

Deliberately generic: every scenario below uses a made-up Deployment name
(`some-service`) rather than `citizen-service`, to prove the mechanism is not
hardcoded to one application.
"""
from __future__ import annotations

import asyncio

import pytest

from app.lifecycle import k8s_watch


class _FakeK8sList:
    """Minimal fake exposing only what k8s_watch.py calls."""

    def __init__(self, deployments=None, available=True):
        self.available = available
        self._deployments = deployments or []
        self._full = {}

    def set_deployment(self, name, desired, available_replicas, annotations=None):
        self._deployments = [
            d for d in self._deployments if d["name"] != name
        ] + [
            {
                "name": name,
                "desired_replicas": desired,
                "available_replicas": available_replicas,
                "images": [],
            }
        ]
        self._full[name] = {
            "name": name,
            "desired_replicas": desired,
            "available_replicas": available_replicas,
            "annotations": annotations or {},
        }

    def remove_deployment(self, name):
        self._deployments = [d for d in self._deployments if d["name"] != name]
        self._full.pop(name, None)

    async def list_deployments(self, namespace):
        return list(self._deployments)

    async def get_deployment(self, namespace, name):
        return self._full.get(name)


class _FakeManager:
    def __init__(self):
        self.alerts: list[dict] = []
        self.resolved: list[dict] = []

    def handle_alert(self, normalised, environment=None):
        self.alerts.append(normalised)

    def handle_resolved(self, normalised, environment=None):
        self.resolved.append(normalised)


class _FakeCtx:
    def __init__(self, k8s):
        self.k8s = k8s


class _FakeKubeCfg:
    namespace = "some-namespace"


class _FakeEnvironment:
    kubernetes = _FakeKubeCfg()


# ---------------------------------------------------------------------------
# Test 1 — Deployment scaled to zero (no annotation) -> eventually flagged
# ---------------------------------------------------------------------------
def test_scaled_to_zero_is_flagged_after_debounce_no_hardcoded_app():
    dep = {"name": "some-service", "desired_replicas": 0, "available_replicas": 0}
    unhealthy, reason = k8s_watch._is_unhealthy(dep, "sentinel.sre/expected-scale-zero")
    assert unhealthy is True
    assert "0 desired replicas" in reason


# ---------------------------------------------------------------------------
# Test 2 — Deployment desired > 0 but no Pod (available == 0)
# ---------------------------------------------------------------------------
def test_desired_positive_no_pods_is_flagged():
    dep = {"name": "some-service", "desired_replicas": 1, "available_replicas": 0}
    unhealthy, reason = k8s_watch._is_unhealthy(dep, "sentinel.sre/expected-scale-zero")
    assert unhealthy is True
    assert "desired=1" in reason and "available=0" in reason


# ---------------------------------------------------------------------------
# Test 3 — desired > 0, available < desired (partial: e.g. CrashLoop on 1/2)
# ---------------------------------------------------------------------------
def test_partial_unavailability_is_flagged():
    dep = {"name": "some-service", "desired_replicas": 2, "available_replicas": 1}
    unhealthy, _ = k8s_watch._is_unhealthy(dep, "sentinel.sre/expected-scale-zero")
    assert unhealthy is True


# ---------------------------------------------------------------------------
# Test 4 — healthy Deployment is never flagged
# ---------------------------------------------------------------------------
def test_healthy_deployment_not_flagged():
    dep = {"name": "some-service", "desired_replicas": 2, "available_replicas": 2}
    unhealthy, _ = k8s_watch._is_unhealthy(dep, "sentinel.sre/expected-scale-zero")
    assert unhealthy is False


# ---------------------------------------------------------------------------
# Test 6 — intentional scale-to-zero (annotation present) is never flagged
# ---------------------------------------------------------------------------
def test_intentional_scale_to_zero_is_not_flagged():
    dep = {
        "name": "some-service",
        "desired_replicas": 0,
        "available_replicas": 0,
        "annotations": {"sentinel.sre/expected-scale-zero": "true"},
    }
    unhealthy, _ = k8s_watch._is_unhealthy(dep, "sentinel.sre/expected-scale-zero")
    assert unhealthy is False


def test_scaling_down_in_progress_not_yet_flagged():
    """desired==0 but a Pod is still terminating (available>0): not zero yet."""
    dep = {"name": "some-service", "desired_replicas": 0, "available_replicas": 1}
    unhealthy, _ = k8s_watch._is_unhealthy(dep, "sentinel.sre/expected-scale-zero")
    assert unhealthy is False


def test_deployment_with_no_desired_replicas_field_is_skipped():
    """Defensive: a malformed/partial dict must never raise or be flagged."""
    dep = {"name": "some-service", "available_replicas": 0}
    unhealthy, _ = k8s_watch._is_unhealthy(dep, "sentinel.sre/expected-scale-zero")
    assert unhealthy is False


# ---------------------------------------------------------------------------
# End-to-end debounce + incident signalling + auto-resolution, via _evaluate
# ---------------------------------------------------------------------------
def test_evaluate_signals_incident_only_after_debounce_then_resolves():
    async def _run():
        k8s = _FakeK8sList()
        k8s.set_deployment("some-service", desired=1, available_replicas=0)
        manager = _FakeManager()
        environment = _FakeEnvironment()
        ctx = _FakeCtx(k8s)
        state: dict = {}

        # First poll: condition just started, debounce not elapsed -> no alert.
        await k8s_watch._evaluate(
            ctx, manager, environment, state,
            debounce_seconds=60, expected_zero_annotation="sentinel.sre/expected-scale-zero",
        )
        assert manager.alerts == []

        # Simulate time passing past the debounce window by manipulating the
        # tracked state directly (unit-level; the real loop uses time.time()).
        st = state["some-service"]
        st.unhealthy_since -= 61

        await k8s_watch._evaluate(
            ctx, manager, environment, state,
            debounce_seconds=60, expected_zero_annotation="sentinel.sre/expected-scale-zero",
        )
        assert len(manager.alerts) == 1
        assert manager.alerts[0]["alertname"] == k8s_watch.WATCH_ALERTNAME
        assert manager.alerts[0]["app"] == "some-service"

        # A third poll while still unhealthy must NOT re-signal (dedup is the
        # IncidentManager's job; this sensor must not spam it every interval).
        await k8s_watch._evaluate(
            ctx, manager, environment, state,
            debounce_seconds=60, expected_zero_annotation="sentinel.sre/expected-scale-zero",
        )
        assert len(manager.alerts) == 1

        # Recovery: Deployment becomes healthy -> a resolved notification is
        # sent through the SAME handle_resolved() path a real Alertmanager
        # "resolved" webhook would use.
        k8s.set_deployment("some-service", desired=1, available_replicas=1)
        await k8s_watch._evaluate(
            ctx, manager, environment, state,
            debounce_seconds=60, expected_zero_annotation="sentinel.sre/expected-scale-zero",
        )
        assert len(manager.resolved) == 1
        assert manager.resolved[0]["status"] == "resolved"

    asyncio.run(_run())


def test_evaluate_never_flags_intentional_zero_end_to_end():
    async def _run():
        k8s = _FakeK8sList()
        k8s.set_deployment(
            "some-batch-job", desired=0, available_replicas=0,
            annotations={"sentinel.sre/expected-scale-zero": "true"},
        )
        manager = _FakeManager()
        environment = _FakeEnvironment()
        ctx = _FakeCtx(k8s)
        state: dict = {}
        for _ in range(3):
            await k8s_watch._evaluate(
                ctx, manager, environment, state,
                debounce_seconds=0, expected_zero_annotation="sentinel.sre/expected-scale-zero",
            )
        assert manager.alerts == []

    asyncio.run(_run())


def test_evaluate_skips_polling_when_kubernetes_unavailable_via_run_loop():
    """`run_k8s_watch` must not call list_deployments at all while
    `ctx.k8s.available` is False (mirrors main.py's own
    kubernetes_unavailable_at_startup handling)."""

    async def _run():
        k8s = _FakeK8sList(available=False)
        k8s.set_deployment("some-service", desired=1, available_replicas=0)
        manager = _FakeManager()
        environment = _FakeEnvironment()
        ctx = _FakeCtx(k8s)

        task = asyncio.create_task(
            k8s_watch.run_k8s_watch(
                ctx, manager, environment,
                poll_interval_seconds=0.01, debounce_seconds=0,
                expected_zero_annotation="sentinel.sre/expected-scale-zero",
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert manager.alerts == []

    asyncio.run(_run())
