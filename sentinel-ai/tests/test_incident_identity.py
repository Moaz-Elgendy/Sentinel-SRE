"""Unit tests for incident identity and material-evidence detection."""
from __future__ import annotations

import pytest

from app.lifecycle.detection import failure_class_for
from app.lifecycle.evidence_signature import (
    compute_signature,
    material_changes,
    normalise_log_pattern,
)
from app.models.incident import Evidence, compute_incident_key


def key(**over):
    base = dict(environment_id="env-1", application_id="app-1", namespace="citizen-portal",
                app="citizen-service", failure_class="http_errors")
    base.update(over)
    return compute_incident_key(**base)


def test_incident_key_is_stable_and_scoped():
    assert key() == key()
    assert key() != key(app="notification-service")
    assert key() != key(failure_class="latency")
    assert key() != key(environment_id="env-2")
    assert key() != key(namespace="other")


def test_two_rules_for_one_symptom_share_a_failure_class():
    assert failure_class_for("HighHTTPErrorRate") == failure_class_for("ChaosForcedHTTPFailures")
    assert failure_class_for("ServiceDown") == failure_class_for("PodCrashLooping")
    assert failure_class_for("HighHTTPErrorRate") != failure_class_for("HighRequestLatency")
    # unknown alerts never merge with anything
    assert failure_class_for("SomethingNew") == "alert:somethingnew"


def test_log_patterns_ignore_volatile_tokens():
    a = normalise_log_pattern("timeout after 3021ms calling 10.0.0.7:8000 req 9f8e7d6c5b4a (x12)")
    b = normalise_log_pattern("timeout after 3187ms calling 10.0.0.9:8000 req 0a1b2c3d4e5f (x3)")
    assert a == b


def ev(**kw):
    base = dict(error_rate=0.6, p95_latency_seconds=0.3, cpu_cores=0.1, memory_bytes=1e8, up=1.0,
                restart_count_total=0)
    base.update(kw)
    return Evidence(**base)


def test_identical_and_jittery_evidence_is_not_material():
    base = compute_signature(ev(log_sample_messages=["db timeout 100ms (x4)"]))
    same = compute_signature(ev(error_rate=0.62, memory_bytes=1.1e8, log_sample_messages=["db timeout 250ms (x40)"]))
    assert material_changes(base, same) == []


@pytest.mark.parametrize(
    "change,expected",
    [
        (dict(error_rate=0.1), "error rate"),
        (dict(memory_bytes=3e8), "memory"),
        (dict(up=0.0), "availability"),
        (dict(restart_count_total=4), "restart"),
        (dict(k8s_events=[{"type": "Warning", "reason": "BackOff"}]), "Warning event"),
        (dict(log_sample_messages=["brand new failure mode"]), "log error pattern"),
    ],
)
def test_material_changes_are_detected(change, expected):
    base = compute_signature(ev())
    reasons = material_changes(base, compute_signature(ev(**change)))
    assert reasons and any(expected in r for r in reasons)


def test_warning_events_ageing_out_is_not_new_information():
    with_event = compute_signature(ev(k8s_events=[{"type": "Warning", "reason": "BackOff"}]))
    without = compute_signature(ev())
    assert material_changes(with_event, without) == []


def test_no_baseline_never_invents_a_change():
    assert material_changes(None, compute_signature(ev())) == []
    assert material_changes(compute_signature(ev()), None) == []
