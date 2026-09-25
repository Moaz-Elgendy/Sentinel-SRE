"""
Regression tests for the incident engine: incident identity, correlation,
controlled concurrency, escalation semantics, bounded retries, provider
failure containment and restart persistence.

Every scenario drives the REAL IncidentManager + Orchestrator + policy/RCA/
remediation engines against a real SQLite file (see engine_harness.py); only
the outside world is faked. Chaos-style symptoms are given as *evidence*
(metrics), never as a diagnosis — RCA has to reach its conclusion itself.
"""
from __future__ import annotations

import logging
import time

import httpx
import pytest

from app.lifecycle import detection, rca
from app.lifecycle.incident_manager import CorrelationKind
from app.models.incident import (
    EscalationReason,
    Incident,
    IncidentStatus,
    RemediationAction,
    ValidationOutcome,
)
from app.reasoning.base import Reasoner
from app.reasoning.health import ReasonerHealth
from tests.conftest import executed_attempt, make_plan
from tests.engine_harness import (
    Engine,
    FakeValidator,
    Rendezvous,
    VersionedK8s,
    http_fault,
    unexplained_errors,
)

MEMORY_LEAK = {
    "error_rate": 0.0, "request_rate": 5.0, "p95": 0.2, "cpu": 0.1,
    "memory": 3.5e8, "memory_growth": 2.0e8, "up": 1.0, "chaos_state": {},
}
# RCA is ~0.92 confident this is a memory leak; requiring 0.95 for a restart
# makes policy REFUSE it. (Raising a threshold never weakens safety.)
STRICT_RESTART = {"confidence_threshold_restart": 0.95}


@pytest.fixture
def make_engine(tmp_path):
    engines: list[Engine] = []

    def factory(by_app, **kw):
        e = Engine(tmp_path, by_app, **kw)
        engines.append(e)
        return e

    yield factory
    for e in engines:
        e.close()


def kinds(engine, incident_id=None):
    return [
        e.get("event_kind")
        for e in engine.events
        if e.get("event_kind") and (incident_id is None or e.get("incident_id") == incident_id)
    ]


def escalation_entries(record):
    return [t for t in record["timeline"] if t["phase"] == "escalation"]


# ---------------------------------------------------------------------------
# 1. Two simultaneous, independent incidents
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_two_simultaneous_incidents_are_independent(make_engine):
    e = make_engine(
        {"citizen-service": http_fault(), "notification-service": MEMORY_LEAK}
    )
    # Both lifecycles must reach their first Prometheus call TOGETHER. If the
    # second were queued behind the first (the old BackgroundTasks behaviour)
    # it could never arrive and this test fails on the rendezvous timeout.
    e.prom.barrier = Rendezvous(2)

    a = e.send("HighHTTPErrorRate", "citizen-service", pod="cs-1")
    b = e.send("MemoryLeakSuspected", "notification-service", pod="ns-1")
    assert a.created and b.created
    assert a.incident_id != b.incident_id
    await e.idle()

    ia, ib = e.get(a.incident_id), e.get(b.incident_id)
    assert ia["status"] == "resolved" and ib["status"] == "resolved"
    assert ia["app"] == "citizen-service" and ib["app"] == "notification-service"
    assert ia["fingerprint"] != ib["fingerprint"]

    # own RCA, own evidence, own confidence
    assert ia["hypothesis"]["root_cause"] == "chaos_http_fault"
    assert ib["hypothesis"]["root_cause"] == "memory_leak"
    assert ia["evidence"]["error_rate"] == 0.9 and ib["evidence"]["error_rate"] == 0.0
    assert ia["hypothesis"]["confidence"] != ib["hypothesis"]["confidence"]

    # no cross-incident evidence: the cluster-wide notification delivery
    # series is failing badly, and citizen-service's incident must not have it
    assert ia["evidence"]["notification_deliveries"] == {}
    assert ib["evidence"]["notification_deliveries"] != {}

    # Test 6 — each remediation is attached to its OWN incident and target
    a_actions = [x["plan"]["params"]["deployment"] for x in ia["attempts"] if x["result"]]
    b_actions = [x["plan"]["params"]["deployment"] for x in ib["attempts"] if x["result"]]
    assert a_actions == ["citizen-service"] and b_actions == ["notification-service"]
    assert [w[1]["name"] for w in e.k8s.writes] == ["notification-service"]
    assert e.chaos.resets == [e.settings.citizen_service_url]

    # real activity for Sentinel Live, per incident
    for inc_id in (a.incident_id, b.incident_id):
        got = kinds(e, inc_id)
        assert "created" in got and "investigation_started" in got
        assert {"evidence_prometheus", "evidence_loki", "evidence_kubernetes"} <= set(got)


# ---------------------------------------------------------------------------
# 2. Repeated identical alerts
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_repeated_identical_alert_never_creates_another_incident(make_engine):
    e = make_engine({"citizen-service": http_fault()})
    first = e.send("HighHTTPErrorRate", "citizen-service", pod="cs-1", fingerprint="fp-1")
    repeats = [e.send("HighHTTPErrorRate", "citizen-service", pod="cs-1", fingerprint="fp-1") for _ in range(5)]
    assert all(r.kind is CorrelationKind.JOINED_ACTIVE for r in repeats)
    assert {r.incident_id for r in repeats} == {first.incident_id}
    await e.idle()
    # ...and the tail of the alert after it resolved
    tail = [e.send("HighHTTPErrorRate", "citizen-service", pod="cs-1", fingerprint="fp-1") for _ in range(3)]
    assert all(t.kind is CorrelationKind.ABSORBED_RESOLVED for t in tail)

    assert len(e.incidents()) == 1
    rec = e.get(first.incident_id)
    assert rec["firing_count"] == 9 and rec["suppressed_repeats"] == 8
    # counted, but the audit trail is not flooded
    assert len([t for t in rec["timeline"] if "repeat firing" in t["message"]]) <= 3
    assert len(e.chaos.resets) == 1  # ONE remediation, not one per alert
    assert "duplicate_correlated" in kinds(e, first.incident_id)


@pytest.mark.asyncio
async def test_pod_replacement_or_second_alert_rule_is_still_the_same_incident(make_engine):
    """Alertmanager's fingerprint changes with the pod label; the incident
    identity must not. Two rules for the same 5xx symptom are one problem."""
    e = make_engine({"citizen-service": unexplained_errors()})
    a = e.send("HighHTTPErrorRate", "citizen-service", pod="cs-old", fingerprint="fp-old")
    b = e.send("ChaosForcedHTTPFailures", "citizen-service", pod="cs-new", fingerprint="fp-new")
    assert b.kind is CorrelationKind.JOINED_ACTIVE and b.incident_id == a.incident_id
    await e.idle()
    assert len(e.incidents()) == 1
    assert len(e.get(a.incident_id)["alert_fingerprints"]) == 2


@pytest.mark.asyncio
async def test_different_failures_do_not_over_correlate(make_engine):
    e = make_engine({"citizen-service": unexplained_errors(), "notification-service": MEMORY_LEAK})
    ids = {
        e.send("HighHTTPErrorRate", "citizen-service").incident_id,
        e.send("MemoryLeakSuspected", "notification-service").incident_id,
        e.send("HighRequestLatency", "citizen-service").incident_id,  # other CLASS, same app
    }
    assert len(ids) == 3
    await e.idle()


# ---------------------------------------------------------------------------
# 3. Escalation is a controlled state, not "try again"
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_escalation_records_why_and_identical_evidence_never_reescalates(make_engine):
    e = make_engine(
        {"notification-service": MEMORY_LEAK},
        settings_overrides=STRICT_RESTART,
    )
    d = e.send("MemoryLeakSuspected", "notification-service", pod="ns-1")
    await e.idle()
    rec = e.get(d.incident_id)
    assert rec["status"] == "escalated"
    assert rec["escalation_reason"] == "no_safe_action"

    er = rec["escalation_record"]
    assert er["incident_id"] == d.incident_id
    assert er["root_cause"] == "memory_leak" and er["confidence"] == pytest.approx(0.92)
    assert er["at"] > 0 and er["evidence_signature"] and er["evidence_used"]["memory_bytes"] == 3.5e8
    (rejected,) = er["rejected_actions"]
    assert rejected["action"] == "restart_deployment"
    assert rejected["required_confidence"] == 0.95
    assert rejected["denial_reason"] == "confidence_too_low"
    assert er["policy_reasons"] == ["confidence_too_low"]
    assert e.k8s.writes == []  # policy was not bypassed

    before_calls = len(e.prom.calls)
    n_escalations = len(escalation_entries(rec))
    for _ in range(6):  # Alertmanager keeps repeating the same alert
        again = e.send("MemoryLeakSuspected", "notification-service", pod="ns-1")
        assert again.kind is CorrelationKind.JOINED_ESCALATED
        assert again.incident_id == d.incident_id
        await e.idle()

    rec2 = e.get(d.incident_id)
    assert len(e.incidents()) == 1                       # no incident spam
    assert rec2["status"] == "escalated"                 # still waiting for a human
    assert len(escalation_entries(rec2)) == n_escalations  # NOT escalated again
    assert rec2["reopen_count"] == 0 and not rec2["escalation_history"]
    assert e.k8s.writes == []
    # At most ONE cheap evidence re-check happened (and found nothing new);
    # no RCA / decision / policy work was repeated.
    assert len(e.prom.calls) - before_calls <= 3
    assert "reconsider_no_change" in kinds(e, d.incident_id)
    assert kinds(e, d.incident_id).count("reconsider_started") == 1
    assert "duplicate_correlated_escalated" in kinds(e, d.incident_id)


@pytest.mark.asyncio
async def test_non_reconsiderable_escalation_never_reprobes(make_engine):
    """Once Sentinel has EXECUTED an action that did not fix things (or hit
    the action cap / an internal error) it is out of automatic options: repeats
    are absorbed with no evidence collection at all."""
    e = make_engine({"citizen-service": http_fault()}, validator=FakeValidator(ValidationOutcome.FAILED))
    d = e.send("HighHTTPErrorRate", "citizen-service")
    await e.idle()
    rec = e.get(d.incident_id)
    assert rec["status"] == "escalated"
    assert any(a["result"] for a in rec["attempts"])  # it DID act, and it did not work
    before = len(e.prom.calls)
    for _ in range(4):
        assert e.send("HighHTTPErrorRate", "citizen-service").reconsideration_scheduled is False
    await e.idle()
    assert len(e.prom.calls) == before and len(e.incidents()) == 1


# ---------------------------------------------------------------------------
# 4. Escalated incident + materially new evidence
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_material_new_evidence_reopens_the_same_incident_once(make_engine):
    e = make_engine(
        {"notification-service": MEMORY_LEAK},
        settings_overrides={**STRICT_RESTART, "escalated_reconsider_min_interval_seconds": 0},
    )
    d = e.send("MemoryLeakSuspected", "notification-service", pod="ns-1")
    await e.idle()
    assert e.get(d.incident_id)["status"] == "escalated"

    e.k8s.set_revision(4)  # someone deployed a new revision
    r = e.send("MemoryLeakSuspected", "notification-service", pod="ns-2")
    assert r.reconsideration_scheduled
    await e.idle()

    rec = e.get(d.incident_id)
    assert rec["id"] == d.incident_id and len(e.incidents()) == 1  # same incident
    assert rec["reopen_count"] == 1
    assert len(rec["escalation_history"]) == 1                     # the past is kept
    reopen = [t for t in rec["timeline"] if "reopened for re-investigation" in t["message"]]
    assert len(reopen) == 1 and "new deployment" in reopen[0]["message"]
    assert rec["status"] == "escalated"  # strict policy still refuses: no bypass
    assert "reopened" in kinds(e, d.incident_id)

    # the SAME (now baseline) evidence arriving again does not reopen it again
    e.send("MemoryLeakSuspected", "notification-service", pod="ns-2")
    await e.idle()
    assert e.get(d.incident_id)["reopen_count"] == 1


@pytest.mark.asyncio
async def test_jitter_inside_a_band_is_not_material(make_engine):
    e = make_engine(
        {"notification-service": dict(MEMORY_LEAK)},
        settings_overrides={**STRICT_RESTART, "escalated_reconsider_min_interval_seconds": 0},
    )
    d = e.send("MemoryLeakSuspected", "notification-service")
    await e.idle()
    e.prom.by_app["notification-service"]["memory"] = 3.6e8      # +3%, same log2 band
    e.prom.by_app["notification-service"]["p95"] = 0.21           # same latency band
    e.send("MemoryLeakSuspected", "notification-service")
    await e.idle()
    assert e.get(d.incident_id)["reopen_count"] == 0
    assert "reconsider_no_change" in kinds(e, d.incident_id)


@pytest.mark.asyncio
async def test_automatic_reopens_are_hard_bounded(make_engine):
    e = make_engine(
        {"notification-service": MEMORY_LEAK},
        settings_overrides={
            **STRICT_RESTART,
            "escalated_reconsider_min_interval_seconds": 0,
            "max_incident_reopens": 1,
        },
    )
    d = e.send("MemoryLeakSuspected", "notification-service")
    await e.idle()
    e.k8s.set_revision(4)
    e.send("MemoryLeakSuspected", "notification-service")
    await e.idle()
    assert e.get(d.incident_id)["reopen_count"] == 1

    for revision in (5, 6, 7):  # keeps changing; budget is spent
        e.k8s.set_revision(revision)
        r = e.send("MemoryLeakSuspected", "notification-service")
        assert r.reconsideration_scheduled is False
        await e.idle()
    rec = e.get(d.incident_id)
    assert rec["reopen_count"] == 1 and rec["status"] == "escalated"
    assert len([t for t in rec["timeline"] if "retry limit" in t["message"]]) == 1
    assert kinds(e, d.incident_id).count("reinvestigation_blocked") == 3
    assert len(e.incidents()) == 1


# ---------------------------------------------------------------------------
# 5. Recurrence after resolution
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_resolved_incident_recurrence_is_a_new_occurrence_not_history_rewrite(make_engine):
    e = make_engine({"citizen-service": http_fault()})
    first = e.send("HighHTTPErrorRate", "citizen-service")
    await e.idle()
    assert e.get(first.incident_id)["status"] == "resolved"

    tail = e.send("HighHTTPErrorRate", "citizen-service")          # rate[5m] tail
    assert tail.kind is CorrelationKind.ABSORBED_RESOLVED and len(e.incidents()) == 1

    e.advance(e.settings.incident_recurrence_gap_seconds + 60)      # a real recovery period
    again = e.send("HighHTTPErrorRate", "citizen-service")
    assert again.kind is CorrelationKind.RECURRENCE and again.created
    assert again.incident_id != first.incident_id
    new = again.incident
    assert new.occurrence == 2 and new.previous_incident_id == first.incident_id
    await e.idle()
    assert len(e.incidents()) == 2
    assert e.get(first.incident_id)["status"] == "resolved"        # history untouched


# ---------------------------------------------------------------------------
# 6. Same Deployment, two incidents: one remediation, not two
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_two_incidents_on_one_deployment_do_not_double_remediate(make_engine):
    e = make_engine({"citizen-service": http_fault()}, heal_on_chaos_reset=True)
    e.prom.barrier = Rendezvous(2)
    a = e.send("HighHTTPErrorRate", "citizen-service")
    b = e.send("HighRequestLatency", "citizen-service")  # different failure class
    assert a.incident_id != b.incident_id
    await e.idle()
    # One remediation on the Deployment, not two. The loser waited for the
    # per-Deployment lock, saw that another incident had acted after its own
    # evidence was collected, and re-investigated instead of acting on a stale
    # picture (the reset really did clear the fault: see HealingChaos).
    assert len(e.chaos.resets) == 1
    assert e.k8s.writes == []
    loser = [e.get(i) for i in (a.incident_id, b.incident_id) if not any(x["result"] for x in e.get(i)["attempts"])]
    assert loser and any("refreshing evidence" in t["message"] for t in loser[0]["timeline"])
    statuses = sorted(e.get(i)["status"] for i in (a.incident_id, b.incident_id))
    assert "resolved" in statuses


# ---------------------------------------------------------------------------
# Bounded retries — nothing loops forever
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_failed_validation_cannot_loop_forever(make_engine):
    e = make_engine({"citizen-service": http_fault()}, validator=FakeValidator(ValidationOutcome.FAILED))
    d = e.send("HighHTTPErrorRate", "citizen-service")
    await e.idle()  # would time out if the lifecycle looped
    rec = e.get(d.incident_id)
    assert rec["status"] == "escalated" and rec["escalation_reason"]
    executed = [a for a in rec["attempts"] if a["result"]]
    assert len(executed) <= e.settings.max_actions_per_incident


@pytest.mark.asyncio
async def test_wall_clock_deadline_escalates_with_explicit_reason(make_engine):
    e = make_engine(
        {"citizen-service": http_fault()},
        validator=FakeValidator(ValidationOutcome.FAILED),
        settings_overrides={"max_lifecycle_seconds": -1},
    )
    d = e.send("HighHTTPErrorRate", "citizen-service")
    await e.idle()
    rec = e.get(d.incident_id)
    assert rec["status"] == "escalated"
    assert rec["escalation_reason"] == EscalationReason.LIFECYCLE_TIMEOUT.value
    assert len(e.chaos.resets) == 1


# ---------------------------------------------------------------------------
# Provider failure is a condition, never an incident
# ---------------------------------------------------------------------------
class DeadProvider(Reasoner):
    label = "dead:model"

    def __init__(self):
        self.calls = 0

    async def complete_json(self, system_prompt, user_prompt):
        self.calls += 1
        self.report_failure("HTTP 404 NOT_FOUND from https://example/models/dead:generateContent", 404)
        return None


@pytest.mark.asyncio
async def test_reasoner_failure_creates_no_incident_and_does_not_loop(make_engine):
    provider = DeadProvider()
    e = make_engine({"citizen-service": http_fault()}, reasoner=provider)
    d = e.send("HighHTTPErrorRate", "citizen-service")
    await e.idle()
    rec = e.get(d.incident_id)
    assert len(e.incidents()) == 1                        # the provider is not an incident
    assert rec["hypothesis"]["llm_status"] in ("call_failed", "reasoner_unavailable")
    assert rec["hypothesis"]["confidence"] == pytest.approx(0.96)  # not inflated to compensate
    assert rec["status"] == "resolved"                    # rules-only path still safe and complete
    assert any("REASONER_UNAVAILABLE" in t["message"] for t in rec["timeline"])
    assert provider.calls == 1                            # no retry storm


@pytest.mark.asyncio
async def test_circuit_breaker_stops_calling_a_dead_provider():
    provider = DeadProvider()
    provider.health = ReasonerHealth(failure_threshold=2, cooldown_seconds=60)
    inc = Incident(id="I", fingerprint="f", alertname="HighHTTPErrorRate",
                   severity=detection.Severity.CRITICAL, app="citizen-service",
                   namespace="citizen-portal")
    from app.models.incident import Evidence, Hypothesis, RootCause

    ev = Evidence(error_rate=0.6, up=1.0)
    base = Hypothesis(
        root_cause=RootCause.UNKNOWN, confidence=0.6, reasoning="rules",
        recommended_action=RemediationAction.ESCALATE,
    )
    for _ in range(5):
        out = await rca.enrich_with_llm(inc, ev, base, provider)
        assert out.confidence == base.confidence
    assert provider.calls == 2                            # 3rd..5th were skipped
    assert out.llm_status == "reasoner_unavailable"
    assert provider.health.snapshot()["status"] == "reasoner_unavailable"


@pytest.mark.asyncio
async def test_gemini_404_is_diagnosable_and_never_leaks_the_key(monkeypatch, caplog):
    import app.reasoning.gemini_reasoner as g

    secret = "AIzaSy-SUPER-SECRET-KEY"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(404, json={"error": {
                "code": 404, "status": "NOT_FOUND",
                "message": "models/gemini-2.0-flash is no longer available to new users."}})
        return httpx.Response(200, json={"models": [
            {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]},
        ]})

    real = httpx.AsyncClient
    monkeypatch.setattr(g.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    caplog.set_level(logging.WARNING)
    r = g.GeminiReasoner(api_key=secret, model="gemini-2.0-flash")
    assert await r.complete_json("s", "u") is None
    assert await r.complete_json("s", "u") is None

    posts = [q for q in seen if q.method == "POST"]
    assert posts[0].url.path == "/v1beta/models/gemini-2.0-flash:generateContent"
    assert "key=" not in str(posts[0].url) and posts[0].headers["x-goog-api-key"] == secret
    assert len([q for q in seen if q.method == "GET"]) == 1   # ListModels once per model

    (err, _err2) = [x for x in caplog.records if x.getMessage() == "gemini_call_http_error"]
    assert err.status_code == 404 and err.google_status == "NOT_FOUND"
    assert err.model == "gemini-2.0-flash" and err.api_version == "v1beta"
    assert err.endpoint.endswith("gemini-2.0-flash:generateContent")
    listed = [x for x in caplog.records if x.getMessage() == "gemini_available_models"]
    assert listed and listed[0].generate_content_models == ["gemini-2.5-flash"]
    assert secret not in "".join(f"{x.getMessage()} {x.__dict__}" for x in caplog.records)
    snap = r.health.snapshot()
    assert snap["last_status_code"] == 404 and snap["consecutive_failures"] == 2


def test_groq_is_selected_through_the_same_factory():
    from app.core.config import Settings
    from app.reasoning.factory import build_reasoner
    from app.reasoning.groq_reasoner import GroqReasoner
    from app.reasoning.openai_reasoner import OpenAIReasoner

    s = Settings(llm_provider="groq", groq_api_key="k")
    r = build_reasoner(s)
    assert isinstance(r, GroqReasoner) and isinstance(r, Reasoner)
    assert r.label == "groq:openai/gpt-oss-20b" and "api.groq.com" in r.base_url
    assert s.llm_enabled
    assert build_reasoner(Settings(llm_provider="groq", groq_api_key="")) is None
    assert isinstance(build_reasoner(Settings(llm_provider="openai", openai_api_key="k")), OpenAIReasoner)
    assert build_reasoner(Settings(llm_provider="gemini", gemini_api_key="k")).label.startswith("gemini:")


# ---------------------------------------------------------------------------
# Persistence: state survives a restart
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_escalated_incident_survives_restart_and_still_deduplicates(tmp_path):
    e1 = Engine(tmp_path, {"notification-service": MEMORY_LEAK}, settings_overrides=STRICT_RESTART)
    d = e1.send("MemoryLeakSuspected", "notification-service")
    await e1.idle()
    assert e1.get(d.incident_id)["status"] == "escalated"
    e1.close()

    e2 = Engine(tmp_path, {"notification-service": MEMORY_LEAK}, settings_overrides=STRICT_RESTART)
    try:
        assert e2.manager.recover_interrupted() == {"resumed": [], "escalated": []}
        again = e2.send("MemoryLeakSuspected", "notification-service")
        assert again.kind is CorrelationKind.JOINED_ESCALATED and again.incident_id == d.incident_id
        await e2.idle()
        assert len(e2.incidents()) == 1
    finally:
        e2.close()


@pytest.mark.asyncio
async def test_interrupted_incident_is_resumed_once_then_escalated(make_engine):
    e = make_engine({"citizen-service": http_fault()})
    norm = detection.normalise_alert(_alert("HighHTTPErrorRate", "citizen-service"))
    inc = detection.build_incident(norm, e.environment)
    inc.status = IncidentStatus.INVESTIGATING          # process died mid-investigation
    e.store.upsert_incident(inc.to_dict())

    out = e.manager.recover_interrupted()
    assert out["resumed"] == [inc.id]
    await e.idle()
    rec = e.get(inc.id)
    assert rec["status"] == "resolved"
    assert any(t["detail"].get("resumed_after_restart") for t in rec["timeline"])

    # crash again: it was already resumed once -> a human decides, no third run
    again = Incident.from_dict(rec)
    again.status = IncidentStatus.INVESTIGATING
    e.store.upsert_incident(again.to_dict())
    resets = len(e.chaos.resets)
    out = e.manager.recover_interrupted()
    assert out == {"resumed": [], "escalated": [inc.id]}
    rec = e.get(inc.id)
    assert rec["status"] == "escalated" and rec["escalation_reason"] == "interrupted_by_restart"
    assert len(e.chaos.resets) == resets


@pytest.mark.asyncio
async def test_interrupted_incident_with_executed_action_is_never_rerun(make_engine):
    e = make_engine({"citizen-service": http_fault()})
    norm = detection.normalise_alert(_alert("HighHTTPErrorRate", "citizen-service"))
    inc = detection.build_incident(norm, e.environment)
    inc.status = IncidentStatus.VALIDATING
    inc.attempts.append(executed_attempt(make_plan(RemediationAction.RESTART_DEPLOYMENT)))
    e.store.upsert_incident(inc.to_dict())

    assert e.manager.recover_interrupted() == {"resumed": [], "escalated": [inc.id]}
    rec = e.get(inc.id)
    assert rec["status"] == "escalated" and "will not repeat them blindly" in rec["escalation_detail"]
    assert e.k8s.writes == [] and e.chaos.resets == []


@pytest.mark.asyncio
async def test_alertmanager_resolved_closes_escalated_incident_as_auto_resolved(make_engine):
    e = make_engine({"notification-service": MEMORY_LEAK}, settings_overrides=STRICT_RESTART)
    d = e.send("MemoryLeakSuspected", "notification-service", pod="ns-1")
    await e.idle()
    out = e.send("MemoryLeakSuspected", "notification-service", pod="ns-9", status="resolved")
    assert out["incident_id"] == d.incident_id
    assert e.get(d.incident_id)["status"] == "auto_resolved"   # not counted as a Sentinel success


def _alert(alertname, app, pod=None):
    from app.models.incident import AlertmanagerAlert

    return AlertmanagerAlert(
        status="firing",
        labels={"alertname": alertname, "app": app, "severity": "critical", "namespace": "citizen-portal"},
        annotations={}, startsAt="2026-09-20T10:00:00Z", fingerprint="x",
    )


# ---------------------------------------------------------------------------
# API level: webhook, activity feed, admin re-run
# ---------------------------------------------------------------------------
def _payload(alertname="HighHTTPErrorRate", app="citizen-service", fp="fp-api", pod="p-1"):
    return {"alerts": [{
        "status": "firing", "fingerprint": fp, "startsAt": "2026-09-20T10:00:00Z",
        "labels": {"alertname": alertname, "app": app, "severity": "critical",
                   "namespace": "citizen-portal", "kubernetes_pod_name": pod},
        "annotations": {"summary": "s"},
    }]}


def _wait_status(client, headers, incident_id, wanted, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        body = client.get(f"/api/incidents/{incident_id}", headers=headers).json()
        if body.get("status") in wanted:
            return body
        time.sleep(0.1)
    raise AssertionError(f"incident never reached {wanted}: {body.get('status')}")


def test_webhook_repeat_is_correlated_and_activity_lists_incidents(gui_client):
    client, login = gui_client
    headers = login(client)
    first = client.post("/api/alerts/webhook", json=_payload()).json()
    assert first["accepted"] == 1
    iid = first["incidents"][0]["incident_id"]

    # same problem again — even with a different pod / Alertmanager fingerprint
    second = client.post("/api/alerts/webhook", json=_payload(fp="fp-other", pod="p-2")).json()
    assert second["accepted"] == 0
    assert second["skipped"][0]["incident_id"] == iid
    assert second["skipped"][0]["correlation"] in ("joined_active", "joined_escalated")

    # a genuinely different problem is its own incident
    other = client.post("/api/alerts/webhook",
                        json=_payload("MemoryLeakSuspected", "notification-service", "fp-n", "n-1")).json()
    assert other["accepted"] == 1 and other["incidents"][0]["incident_id"] != iid

    status = client.get("/api/activity/status", headers=headers).json()
    assert "reasoner" in status and "active_incidents" in status


def test_admin_reinvestigate_only_for_escalated_incident(gui_client):
    client, login = gui_client
    headers = login(client)
    assert client.post("/api/incidents/nope/reinvestigate", headers=headers).status_code == 404
    iid = client.post("/api/alerts/webhook", json=_payload()).json()["incidents"][0]["incident_id"]
    _wait_status(client, headers, iid, {"escalated"})   # no cluster/Prometheus in tests
    ok = client.post(f"/api/incidents/{iid}/reinvestigate", headers=headers)
    assert ok.status_code == 202 and ok.json()["incident_id"] == iid
    body = _wait_status(client, headers, iid, {"escalated"})
    assert any("manual re-investigation" in t["message"] for t in body["timeline"])
    assert client.post(f"/api/incidents/{iid}/reinvestigate").status_code in (401, 403)
