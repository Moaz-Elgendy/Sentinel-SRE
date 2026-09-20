"""
Offline harness for incident-engine tests: REAL IncidentManager, REAL
Orchestrator, REAL SQLite store, REAL policy/decision/RCA/remediation engines;
only the outside world (Prometheus, Loki, Kubernetes, chaos endpoint, GitHub,
Slack, the post-remediation validator) is faked. Nothing here reaches a
network or a cluster.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from app.core.config import Settings
from app.domain.environment import Environment
from app.lifecycle.incident_manager import IncidentManager
from app.lifecycle.orchestrator import Orchestrator, build_context
from app.models.incident import (
    AlertmanagerAlert,
    ValidationOutcome,
    ValidationReport,
)
from app.lifecycle import detection
from app.lifecycle.validation import ValidationThresholds
from app.store.sqlite_store import SQLiteStore
from tests.conftest import FakeChaos, FakeGitHub, FakeKubernetes

METRICS = (
    "error_rate error_count request_rate p95 cpu memory memory_growth up"
).split()


class AppProm:
    """Per-app canned Prometheus. `by_app[app]` is a FakePrometheus-style
    values dict; every call is recorded so tests can prove what was (not)
    queried. `barrier`, if set, makes each lifecycle wait for the others at
    its first metric call — a lifecycle that is serialised behind another one
    can never reach it, so the test fails loudly instead of passing by luck."""

    def __init__(self, by_app: dict[str, dict[str, Any]]):
        self.by_app = by_app
        self.calls: list[tuple[str, str]] = []
        self.barrier: "Rendezvous | None" = None

    def _v(self, app, key, default=None):
        return self.by_app.get(app, {}).get(key, default)

    async def _enter(self, app):
        self.calls.append(("error_rate", app))
        if self.barrier is not None:
            await self.barrier.arrive(f"{app}#{len(self.calls)}")

    async def error_rate(self, app, window="5m"):
        await self._enter(app)
        return self._v(app, "error_rate")

    async def error_count(self, app, window="5m"): return self._v(app, "error_count")
    async def request_rate(self, app, window="5m"): return self._v(app, "request_rate")
    async def p95_latency(self, app, window="5m"): return self._v(app, "p95")
    async def cpu_cores(self, app, window="5m"): return self._v(app, "cpu")
    async def memory_bytes(self, app): return self._v(app, "memory")
    async def memory_growth_bytes(self, app, window="30m"): return self._v(app, "memory_growth")
    async def up(self, app): return self._v(app, "up")
    async def chaos_state(self, app): return self._v(app, "chaos_state", {})
    async def chaos_injections(self, app, window="10m"): return {}

    async def notification_deliveries(self, window="10m"):
        self.calls.append(("notification_deliveries", "*"))
        # Cluster-wide series: deliberately FAILING, to prove a
        # citizen-service incident does not absorb it as its own evidence.
        return {"failed": 50.0, "delivered": 1.0, "failure_rate": 0.98}

    async def notification_dispatch_failures(self, window="10m"): return None


class Rendezvous:
    def __init__(self, n: int, timeout: float = 5.0):
        self.n, self.timeout = n, timeout
        self.seen: set[str] = set()
        self.event = asyncio.Event()

    async def arrive(self, who: str) -> None:
        self.seen.add(who)
        if len(self.seen) >= self.n:
            self.event.set()
        await asyncio.wait_for(self.event.wait(), self.timeout)


class EmptyLoki:
    async def recent_errors(self, app, namespace, start, end, limit=100): return []
    async def access_log_errors(self, app, namespace, start, end, limit=100): return []


class VersionedK8s(FakeKubernetes):
    """FakeKubernetes whose ReplicaSet history a test can change ("someone
    deployed something")."""

    def __init__(self, revision: int = 3):
        super().__init__(
            available=True,
            deployment={"available_replicas": 1, "replicas": 1, "desired_replicas": 1},
        )
        self.set_revision(revision)

    def set_revision(self, revision: int) -> None:
        self._replicasets = [{"revision": revision, "created_at": 0.0, "images": [f"svc:v{revision}"]}]


class FakeSlack:
    enabled = False
    async def post(self, text, blocks=None): return {"sent": False, "skipped": True}


class FakeValidator:
    """Instant validation with a scripted outcome."""

    def __init__(self, outcome: ValidationOutcome = ValidationOutcome.PASSED):
        self.outcome = outcome
        self.thresholds = ValidationThresholds(
            max_error_rate=0.05, max_p95_seconds=1.5, max_cpu_cores=0.9,
            max_memory_bytes=4e8, settle_seconds=0, timeout_seconds=1, poll_interval_seconds=1,
        )
        self.calls = 0

    def is_available_for(self, deployment): return True

    async def validate(self, incident, params, baseline_error_rate=None):
        self.calls += 1
        return ValidationReport(outcome=self.outcome, detail=f"scripted {self.outcome.value}")


class HealingChaos(FakeChaos):
    """A chaos reset that really clears the fault, so evidence gathered after
    it looks the way a recovered service looks (metrics are per app)."""

    def __init__(self, engine):
        super().__init__()
        self.engine = engine

    async def reset(self, base_url):
        out = await super().reset(base_url)
        for app in list(self.engine.prom.by_app):
            if self.engine.settings.base_url_for(app) == base_url:
                self.engine.prom.by_app[app].update(error_rate=0.0, error_count=0.0, chaos_state={}, p95=0.1)
        return out


class Engine:
    def __init__(self, tmp_path, by_app, *, settings_overrides=None, validator=None,
                 k8s=None, reasoner=None, db_name="sentinel.db", heal_on_chaos_reset=False):
        self.settings = Settings(
            sentinel_db_path=str(tmp_path / db_name),
            dry_run=False,
            validation_settle_seconds=0,
            **(settings_overrides or {}),
        )
        self.db_path = str(tmp_path / db_name)
        self.store = SQLiteStore(self.db_path)
        self.store.connect()
        self.environment = Environment.bootstrap_from_settings(self.settings)
        self.ctx = build_context(self.settings, self.store, self.environment)
        self.prom = AppProm(by_app)
        self.k8s = k8s or VersionedK8s()
        self.chaos = HealingChaos(self) if heal_on_chaos_reset else FakeChaos()
        self.validator = validator or FakeValidator()
        self.ctx.prom = self.prom
        self.ctx.loki = EmptyLoki()
        self.ctx.k8s = self.k8s
        self.ctx.chaos = self.chaos
        self.ctx.remediation.k8s = self.k8s
        self.ctx.remediation.chaos = self.chaos
        self.ctx.github = FakeGitHub(enabled=False)
        self.ctx.slack = FakeSlack()
        self.ctx.validator = self.validator
        self.ctx.reasoner = reasoner
        self.orchestrator = Orchestrator(self.ctx)
        self.offset = 0.0
        self.events: list[dict[str, Any]] = []
        self.manager = self._new_manager()
        self.ctx.event_bus = self.manager.event_bus

    def _new_manager(self) -> IncidentManager:
        class Bus:
            def __init__(inner, sink): inner.sink = sink
            def publish(inner, event): inner.sink.append(event)
        m = IncidentManager(
            self.store, self.settings, event_bus=Bus(self.events),
            orchestrator=self.orchestrator, clock=lambda: time.time() + self.offset,
        )
        self.ctx.incident_manager = m
        return m

    def advance(self, seconds: float) -> None:
        self.offset += seconds

    def send(self, alertname, app, *, pod=None, fingerprint=None, status="firing",
             severity="critical", namespace="citizen-portal", starts="2026-09-20T10:00:00Z"):
        labels = {"alertname": alertname, "app": app, "severity": severity, "namespace": namespace}
        if pod:
            labels["kubernetes_pod_name"] = pod
        alert = AlertmanagerAlert(
            status=status, labels=labels,
            annotations={"summary": f"{alertname} on {app}"},
            startsAt=starts, fingerprint=fingerprint or f"{alertname}-{app}-{pod}",
        )
        normalised = detection.normalise_alert(alert)
        if status == "resolved":
            return self.manager.handle_resolved(normalised, self.environment)
        return self.manager.handle_alert(normalised, self.environment)

    async def idle(self, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.manager._tasks and not self.manager.running_ids():
                return
            await asyncio.sleep(0.02)
        raise AssertionError("lifecycles did not finish: " + repr(self.manager.running_ids()))

    def incidents(self, app=None):
        rows = self.store.list_incidents(limit=200)
        return [r for r in rows if app is None or r.get("app") == app]

    def get(self, incident_id):
        return self.store.get_incident(incident_id)

    def close(self):
        self.store.close()


# --- canned situations ------------------------------------------------------
def http_fault(app="citizen-service", pod="cs-1"):
    """Realistic symptoms of a chaos-injected 5xx fault: the evidence, not the
    diagnosis — RCA must reach its conclusion from these numbers."""
    return {
        "error_rate": 0.9, "error_count": 300.0, "request_rate": 5.0, "p95": 0.2,
        "cpu": 0.1, "memory": 1e8, "up": 1.0,
        "chaos_state": {pod: {"chaos_error_rate": 0.9}},
    }


def unexplained_errors(app="citizen-service", error_rate=0.6):
    """Errors with no chaos gauge, no deploy, no resource pressure: RCA has no
    high-confidence story, so policy must refuse to act."""
    return {
        "error_rate": error_rate, "error_count": 120.0, "request_rate": 5.0, "p95": 0.3,
        "cpu": 0.1, "memory": 1e8, "up": 1.0, "chaos_state": {},
    }
