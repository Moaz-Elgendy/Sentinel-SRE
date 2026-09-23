"""
End-to-end proof that lifecycle/learning.py's bias is real: it must be
observable coming out of the FULL engine (IncidentManager + Orchestrator +
RCA + DecisionEngine + PolicyEngine + RemediationEngine, all real, against a
real SQLite file — see tests/engine_harness.py), not just in the pure
`build_bias`/`merge_bias` unit tests in tests/test_learning.py.

The scenario: three incidents (on three different apps, sharing nothing but
the same RCA root cause) whose chosen actions executed but never validated.
A single lifecycle that fails validation retries down the SAME root cause's
action ladder before giving up (see ACTION_LADDER in decision.py), so each of
these three incidents records an outcome row for BOTH candidates this root
cause has: reset_chaos_fault (tried first) and restart_deployment (the
fallback). Per learning.py's own docstring, that history can only ever make
Sentinel MORE conservative — it can never invent an action or unlock one
above a threshold. A fourth, later incident (a fourth app, identical
symptom) proves the effect is real two ways: the store-level stats function
`learning.load_bias` reads its bias from, and — the actually load-bearing
assertion — the real DecisionEngine/PolicyEngine pipeline denying BOTH
candidates for confidence_too_low where a control run with no prior history
executes both in sequence. Same evidence, same RCA confidence, different
decision — the only input that differs between the two runs is what is in
the `action_outcomes` table beforehand.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.incident import ValidationOutcome
from tests.engine_harness import Engine, FakeValidator, http_fault


@pytest.fixture
def make_engine(tmp_path):
    """Same fixture as test_incident_engine.py's own — kept local rather than
    shared, since a shared conftest fixture would tie two test modules'
    lifecycles together for no real benefit here."""
    engines: list[Engine] = []

    def factory(by_app, **kw):
        e = Engine(tmp_path, by_app, **kw)
        engines.append(e)
        return e

    yield factory
    for e in engines:
        e.close()


# chaos_http_fault's fixed RCA confidence (rca.py) is 0.96. The default
# confidence_threshold_chaos_reset is 0.90 — comfortably below 0.96 (so an
# untrained incident authorises reset_chaos_fault immediately) but ABOVE
# 0.96 * MAX_PENALTY_MULTIPLIER (0.816) — so three unvalidated attempts,
# which drive the bias to exactly that floor, flip the verdict. The ladder's
# fallback, restart_deployment, sits at confidence 0.96 - 0.03
# (FALLBACK_DISCOUNT) = 0.93 before any bias, itself just above its own 0.90
# threshold — but a lifecycle that retries the whole ladder within one
# incident (see below) trains restart_deployment's own history too, biasing
# it to 0.93 * 0.85 = 0.7905, also below threshold. These are this
# codebase's real, existing defaults (app/core/config.py), not numbers
# invented for this test.
#
# frontend is deliberately excluded from this scenario: orchestrator.py's
# `_policy_context` hardcodes it as never having a chaos surface ("the
# frontend has no chaos API"), so it can never be a reset_chaos_fault
# candidate regardless of confidence and would not exercise anything.
ALLOWED_DEPLOYMENTS = "citizen-service,notification-service,billing-service,payment-service"


def _apps():
    return {
        "citizen-service": http_fault(pod="cs-1"),
        "notification-service": http_fault(pod="ns-1"),
        "billing-service": http_fault(pod="bs-1"),
        "payment-service": http_fault(pod="pay-1"),
    }


def _executed_actions(record):
    """Every action that actually reached the cluster (result is not None),
    in the order attempted — empty if every candidate was denied outright."""
    return [a["plan"]["action"] for a in record["attempts"] if a["result"] is not None]


def _rejected(record, action):
    return next(
        (r for r in record["escalation_record"]["rejected_actions"] if r["action"] == action),
        None,
    )


@pytest.mark.asyncio
async def test_prior_incidents_bias_a_later_incidents_real_decision(make_engine, monkeypatch):
    # payment-service (the held-out, "observation" app) and billing-service
    # (a third training app, since frontend can't be used — see above) need
    # a chaos surface (base_url_for) to be eligible for reset_chaos_fault at
    # all, exactly like citizen-service/notification-service already have.
    # Patched onto the Settings CLASS (so it applies identically and fairly
    # to both the control and trained engines) rather than invented as a
    # special case.
    real_base_url_for = Settings.base_url_for
    synthetic_chaos_targets = {
        "payment-service": "http://payment-service.citizen-portal.svc.cluster.local",
        "billing-service": "http://billing-service.citizen-portal.svc.cluster.local",
    }

    def patched_base_url_for(self, target):
        if target in synthetic_chaos_targets:
            return synthetic_chaos_targets[target]
        return real_base_url_for(self, target)

    monkeypatch.setattr(Settings, "base_url_for", patched_base_url_for)

    # ---- control: fresh store, zero history, one incident -----------------
    # Proves what an untrained Sentinel does with this exact symptom: try
    # reset_chaos_fault, and when that doesn't validate, retry restart_
    # deployment — both real cluster actions — before escalating.
    control = make_engine(
        _apps(),
        settings_overrides={"allowed_deployments": ALLOWED_DEPLOYMENTS},
        validator=FakeValidator(ValidationOutcome.FAILED),
        db_name="control.db",
    )
    c = control.send("HighHTTPErrorRate", "payment-service", pod="pay-1")
    await control.idle()
    control_rec = control.get(c.incident_id)
    assert control_rec["hypothesis"]["root_cause"] == "chaos_http_fault"
    assert control_rec["hypothesis"]["confidence"] == pytest.approx(0.96)
    assert _executed_actions(control_rec) == ["reset_chaos_fault", "restart_deployment"], (
        "control (no history) must execute both ladder candidates in order — "
        "neither is denied outright without a trained bias"
    )
    assert control_rec["status"] == "escalated"  # validation was fixed to FAILED

    # ---- training: three incidents, three different apps, same real
    # engine/store, each retrying and failing to validate both candidates --
    trained = make_engine(
        _apps(),
        settings_overrides={"allowed_deployments": ALLOWED_DEPLOYMENTS},
        validator=FakeValidator(ValidationOutcome.FAILED),
        db_name="trained.db",
    )
    for app, pod in [("citizen-service", "cs-1"), ("notification-service", "ns-1"), ("billing-service", "bs-1")]:
        d = trained.send("HighHTTPErrorRate", app, pod=pod)
        await trained.idle()
        rec = trained.get(d.incident_id)
        assert rec["status"] == "escalated"
        assert _executed_actions(rec) == ["reset_chaos_fault", "restart_deployment"], (
            f"training incident on {app} must execute (and fail to validate) both "
            "candidates for its outcomes to be recorded at all"
        )

    # The real store-level mechanism learning.load_bias reads from, confirmed
    # directly: three attempts, zero validated, for EACH candidate this root
    # cause's ladder has.
    stats = trained.store.action_stats("chaos_http_fault")
    assert stats["reset_chaos_fault"] == {"attempts": 3, "validated": 0}
    assert stats["restart_deployment"] == {"attempts": 3, "validated": 0}

    # ---- the incident under test: identical symptom, identical settings,
    # same trained store — the ONLY difference from the control run above --
    e = trained.send("HighHTTPErrorRate", "payment-service", pod="pay-1")
    await trained.idle()
    biased_rec = trained.get(e.incident_id)

    # RCA itself is untouched by learning — same evidence, same hypothesis.
    assert biased_rec["hypothesis"]["root_cause"] == "chaos_http_fault"
    assert biased_rec["hypothesis"]["confidence"] == pytest.approx(0.96)

    # But the DECISION is different: BOTH candidates are now denied for
    # confidence_too_low — reset_chaos_fault at 0.96 * 0.85 = 0.816, and
    # restart_deployment at (0.96 - 0.03) * 0.85 = 0.7905, both below the
    # 0.90 threshold each needs.
    reset_denial = _rejected(biased_rec, "reset_chaos_fault")
    assert reset_denial is not None
    assert reset_denial["denial_reason"] == "confidence_too_low"
    assert reset_denial["confidence"] == pytest.approx(0.96 * 0.85, abs=0.005)
    assert reset_denial["required_confidence"] == pytest.approx(0.90)

    restart_denial = _rejected(biased_rec, "restart_deployment")
    assert restart_denial is not None
    assert restart_denial["denial_reason"] == "confidence_too_low"
    assert restart_denial["confidence"] == pytest.approx((0.96 - 0.03) * 0.85, abs=0.005)
    assert restart_denial["required_confidence"] == pytest.approx(0.90)

    # Nothing executed at all — the same symptom that made the control run
    # act twice now makes Sentinel escalate immediately, having proposed
    # nothing the cluster ever saw. This is learning.py's own stated ceiling
    # in its most visible form: it cannot invent a new action once the whole
    # ladder is exhausted, so "more conservative" here means "escalate"
    # rather than "silently do something else unproven".
    assert _executed_actions(biased_rec) == []
    assert biased_rec["status"] == "escalated"
