"""
CORRELATION tests.

The central case here is the one that motivated `new_replicaset_unhealthy`:
a RollingUpdate with the default maxSurge/maxUnavailable=25% creates the new
(bad) pod *before* removing the old (good) one. For a single-replica
Deployment that means `available_replicas == desired_replicas` never stops
being true, even though the new ReplicaSet the rollout just created never
becomes ready. `replicas_unavailable` cannot see this because it only looks
at the Deployment's aggregate counts; the fix looks at the newest
ReplicaSet's own ready/desired counts instead.
"""
from __future__ import annotations

from app.lifecycle.correlation import correlate
from app.models.incident import Evidence, Incident, Severity


def make_incident(alertname: str = "ServiceDown") -> Incident:
    return Incident(
        id="INC-TEST-0001",
        fingerprint="abc123",
        alertname=alertname,
        severity=Severity.CRITICAL,
        app="citizen-service",
        namespace="citizen-portal",
        evidence=Evidence(),
    )


def _correlate(evidence: Evidence, *, now: float = 10_000.0):
    return correlate(
        make_incident(),
        evidence,
        correlation_window_minutes=30,
        cpu_threshold_cores=0.8,
        error_rate_threshold=0.05,
        p95_threshold_seconds=1.0,
        now=now,
    )


def _surge_stuck_rollout_evidence(**overrides) -> Evidence:
    """Reproduce the exact Kubernetes state from the bad-deployment scenario.

    Deployment: 1 desired / 1 updated / 2 total / 1 available / 1 unavailable
    -> available_replicas == desired_replicas, so `replicas_unavailable` is
    False on its own. The new ReplicaSet (revision 20) has 0/1 ready pods
    (Init:InvalidImageName); the previous ReplicaSet (revision 19) still has
    its 1 pod ready and serving traffic.
    """
    evidence = Evidence(
        error_rate=0.0,
        p95_latency_seconds=0.095,
        up=0.0,
        deployment={
            "desired_replicas": 1,
            "available_replicas": 1,
            "ready_replicas": 1,
            "updated_replicas": 1,
            "unavailable_replicas": 1,
        },
        replicaset_history=[
            {
                "revision": 20,
                "created_at": 10_000.0 - 60,  # 1 minute ago
                "replicas": 1,
                "ready_replicas": 0,
                "images": [
                    "ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/"
                    "sentinel-sre-demo/citizen-service:PLACEHOLDER"
                ],
            },
            {
                "revision": 19,
                "created_at": 10_000.0 - 3600,
                "replicas": 1,
                "ready_replicas": 1,
                "images": [
                    "ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/"
                    "sentinel-sre-demo/citizen-service:abc1234"
                ],
            },
        ],
    )
    for key, value in overrides.items():
        setattr(evidence, key, value)
    return evidence


def test_stuck_surge_rollout_is_detected_as_new_replicaset_unhealthy():
    evidence = _surge_stuck_rollout_evidence()
    findings = _correlate(evidence)

    assert findings.recent_deployment is True
    assert findings.replicas_unavailable is False  # the old blind spot
    assert findings.new_replicaset_unhealthy is True


def test_stuck_surge_rollout_now_correlates_with_onset():
    """This is the exact bug report: recent_deployment=True but
    deploy_correlates_with_onset was False despite clear rollout evidence.
    """
    evidence = _surge_stuck_rollout_evidence()
    findings = _correlate(evidence)

    assert findings.deploy_correlates_with_onset is True
    assert findings.image_changed is True
    assert findings.previous_revision == 19
    assert findings.current_revision == 20


def test_chaos_fault_still_beats_a_stuck_looking_rollout():
    """A recent deploy plus an active chaos fault must still NOT correlate,
    even if the ReplicaSet-level shape happens to look unhealthy too."""
    evidence = _surge_stuck_rollout_evidence(
        chaos_state={"citizen-service-abc": {"chaos_db_failure": 1.0}}
    )
    findings = _correlate(evidence)

    assert findings.new_replicaset_unhealthy is True
    assert findings.deploy_correlates_with_onset is False


def test_healthy_rollout_is_not_flagged():
    """A normal rollout where the new ReplicaSet is fully ready must not
    trip the new signal."""
    evidence = _surge_stuck_rollout_evidence()
    evidence.replicaset_history[0]["ready_replicas"] = 1
    findings = _correlate(evidence)

    assert findings.new_replicaset_unhealthy is False
    assert findings.deploy_correlates_with_onset is False


def test_new_replicaset_unhealthy_requires_a_healthy_previous_replicaset():
    """If the previous ReplicaSet has no ready pods either (e.g. a genuine
    full outage that happens to follow a deploy by coincidence, or the very
    first revision), this signal must not fire - there is nothing showing the
    rollout itself is the problem."""
    evidence = _surge_stuck_rollout_evidence()
    evidence.replicaset_history[1]["ready_replicas"] = 0
    findings = _correlate(evidence)

    assert findings.new_replicaset_unhealthy is False


def test_no_previous_revision_does_not_crash_and_does_not_flag():
    evidence = _surge_stuck_rollout_evidence()
    evidence.replicaset_history = [evidence.replicaset_history[0]]
    findings = _correlate(evidence)

    assert findings.previous_revision is None
    assert findings.new_replicaset_unhealthy is False
    assert findings.deploy_correlates_with_onset is False


# ---------------------------------------------------------------------------
# init_container_failing — the real citizen-service scenario: a healthy
# ReplicaSet rollout whose new pod never gets past `migrate-and-seed`.
# ---------------------------------------------------------------------------
def _pod_with_container(
    *,
    name="citizen-service-79c68cbcf5-lwzwk",
    container_name="migrate-and-seed",
    is_init=True,
    waiting_reason=None,
    terminated_reason=None,
    restart_count=3,
):
    return {
        "name": name,
        "phase": "Pending",
        "ready": False,
        "restart_count": restart_count,
        "container_states": [
            {
                "name": container_name,
                "image": "citizen-service:v2",
                "ready": False,
                "restart_count": restart_count,
                "waiting_reason": waiting_reason,
                "terminated_reason": terminated_reason,
                "last_terminated_reason": "Error",
                "exit_code": 1,
                "started_at": None,
                "finished_at": None,
                "is_init": is_init,
            }
        ],
    }


def test_failing_init_container_is_detected_as_its_own_finding():
    """Test 1: an init container waiting with CrashLoopBackOff sets
    init_container_failing, distinct from (and in addition to) crash_looping."""
    pods = [_pod_with_container(waiting_reason="CrashLoopBackOff")]
    evidence = _surge_stuck_rollout_evidence(pods=pods)
    findings = _correlate(evidence)

    assert findings.init_container_failing is True
    assert findings.init_container_name == "migrate-and-seed"
    assert findings.init_container_exit_reason == "CrashLoopBackOff"
    # new_replicaset_unhealthy is computed purely from ReplicaSet-level
    # ready counts and must be unaffected by this change.
    assert findings.new_replicaset_unhealthy is True


def test_healthy_init_container_is_not_flagged():
    """Test 7: a successfully completed init container is not a failure."""
    pods = [
        _pod_with_container(
            waiting_reason=None, terminated_reason="Completed", restart_count=0
        )
    ]
    evidence = _surge_stuck_rollout_evidence(pods=pods)
    findings = _correlate(evidence)

    assert findings.init_container_failing is False
    assert findings.init_container_name is None


def test_normal_container_crash_loop_does_not_set_init_container_failing():
    """Test 6: an ordinary (non-init) container crash loop keeps working
    exactly as before and is never mistaken for an init-container failure."""
    pods = [
        _pod_with_container(
            container_name="citizen-service",
            is_init=False,
            waiting_reason="CrashLoopBackOff",
        )
    ]
    evidence = _surge_stuck_rollout_evidence(pods=pods)
    findings = _correlate(evidence)

    assert findings.crash_looping is True
    assert findings.init_container_failing is False


def test_replicas_unavailable_path_still_works_independently():
    """Sanity check that the pre-existing aggregate-based signal is untouched."""
    evidence = _surge_stuck_rollout_evidence()
    evidence.deployment = {
        "desired_replicas": 2,
        "available_replicas": 1,
        "ready_replicas": 1,
        "updated_replicas": 1,
        "unavailable_replicas": 1,
    }
    # Make the ReplicaSet-level shape healthy so only the aggregate signal
    # is exercised in this test.
    evidence.replicaset_history[0]["ready_replicas"] = 1
    findings = _correlate(evidence)

    assert findings.replicas_unavailable is True
    assert findings.new_replicaset_unhealthy is False
    assert findings.deploy_correlates_with_onset is True


# ---------------------------------------------------------------------------
# Rollback target selection — the live citizen-service/frontend incident:
# `previous = history[1]` blindly, with no image validation, selected an
# already-broken placeholder ReplicaSet as the rollback target.
# ---------------------------------------------------------------------------
REAL_IMAGE_65 = (
    "890608336467.dkr.ecr.eu-central-1.amazonaws.com/sentinel-sre-demo/"
    "citizen-service:6e36eedd0da47e4a24f0aa5a1c534ce6f4954a84"
)
PLACEHOLDER_IMAGE = (
    "ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/sentinel-sre-demo/citizen-service:PLACEHOLDER"
)


def _rs(revision, *, ready_replicas, images, images_valid, created_at=10_000.0):
    return {
        "revision": revision,
        "created_at": created_at,
        "replicas": 1,
        "ready_replicas": ready_replicas,
        "images": images,
        "images_valid": images_valid,
    }


def test_previous_revision_skips_a_placeholder_candidate_to_the_next_valid_one():
    """The exact live incident: current (67) is broken, revision 66 sitting
    immediately below it is ALSO a placeholder/invalid image (created
    outside Sentinel), and revision 65 further back is the real, valid,
    previously-healthy revision. previous_revision must land on 65, not 66."""
    evidence = _surge_stuck_rollout_evidence(
        replicaset_history=[
            _rs(67, ready_replicas=0, images=[PLACEHOLDER_IMAGE], images_valid=False),
            _rs(66, ready_replicas=0, images=[PLACEHOLDER_IMAGE], images_valid=False),
            _rs(65, ready_replicas=1, images=[REAL_IMAGE_65], images_valid=True),
        ]
    )
    findings = _correlate(evidence)

    assert findings.previous_revision == 65
    assert findings.rollback_candidates_skipped == [66]
    assert findings.image_changed is True  # 67's image differs from 65's


def test_previous_revision_skips_multiple_consecutive_invalid_candidates():
    evidence = _surge_stuck_rollout_evidence(
        replicaset_history=[
            _rs(70, ready_replicas=0, images=[PLACEHOLDER_IMAGE], images_valid=False),
            _rs(69, ready_replicas=0, images=[PLACEHOLDER_IMAGE], images_valid=False),
            _rs(68, ready_replicas=0, images=["also-bad:latest"], images_valid=False),
            _rs(67, ready_replicas=1, images=[REAL_IMAGE_65], images_valid=True),
        ]
    )
    findings = _correlate(evidence)

    assert findings.previous_revision == 67
    assert findings.rollback_candidates_skipped == [69, 68]


def test_previous_revision_is_none_when_every_retained_revision_is_invalid():
    """No safe rollback target anywhere in the retained history (e.g.
    revisionHistoryLimit already pruned the last good revision). Must not
    guess — this feeds straight into the existing
    `previous_revision_exists` Policy Engine precondition, which already
    denies rollback (and therefore escalates) when there is nothing safe to
    target."""
    evidence = _surge_stuck_rollout_evidence(
        replicaset_history=[
            _rs(67, ready_replicas=0, images=[PLACEHOLDER_IMAGE], images_valid=False),
            _rs(66, ready_replicas=0, images=[PLACEHOLDER_IMAGE], images_valid=False),
        ]
    )
    findings = _correlate(evidence)

    assert findings.previous_revision is None
    assert findings.rollback_candidates_skipped == [66]


def test_new_replicaset_unhealthy_still_uses_the_true_immediate_predecessor():
    """new_replicaset_unhealthy is a fact about whether THIS rollout is
    stalled, and must keep comparing against the literal history[1] even
    when previous_revision (the rollback TARGET) skips further back — these
    are two different questions and must not be conflated."""
    evidence = _surge_stuck_rollout_evidence(
        replicaset_history=[
            _rs(67, ready_replicas=0, images=[PLACEHOLDER_IMAGE], images_valid=False),
            # history[1]: itself unhealthy (0 ready) AND invalid — proves
            # new_replicaset_unhealthy is computed from this one directly,
            # not from whatever `previous_revision` ends up being.
            _rs(66, ready_replicas=0, images=[PLACEHOLDER_IMAGE], images_valid=False),
            _rs(65, ready_replicas=1, images=[REAL_IMAGE_65], images_valid=True),
        ]
    )
    findings = _correlate(evidence)

    # previous_revision skipped forward to 65, but new_replicaset_unhealthy
    # requires the TRUE previous ReplicaSet (66) to have a ready pod, and it
    # does not (0 ready) — so the stalled-rollout signal must be False here,
    # proving the two computations are genuinely independent.
    assert findings.previous_revision == 65
    assert findings.new_replicaset_unhealthy is False


def test_valid_digest_pinned_candidate_is_accepted():
    digest_image = (
        "890608336467.dkr.ecr.eu-central-1.amazonaws.com/sentinel-sre-demo/"
        "citizen-service@sha256:" + "a" * 64
    )
    evidence = _surge_stuck_rollout_evidence(
        replicaset_history=[
            _rs(67, ready_replicas=0, images=[PLACEHOLDER_IMAGE], images_valid=False),
            _rs(66, ready_replicas=1, images=[digest_image], images_valid=True),
        ]
    )
    findings = _correlate(evidence)
    assert findings.previous_revision == 66


def test_historical_replicaset_with_zero_replicas_but_valid_template_is_still_a_candidate():
    """A scaled-down-to-zero old revision is still a legitimate rollback
    target as far as image validity goes — `images_valid` is about the
    template, not current replica counts."""
    evidence = _surge_stuck_rollout_evidence(
        replicaset_history=[
            _rs(67, ready_replicas=0, images=[PLACEHOLDER_IMAGE], images_valid=False),
            _rs(66, ready_replicas=0, images=[REAL_IMAGE_65], images_valid=True),
        ]
    )
    findings = _correlate(evidence)
    assert findings.previous_revision == 66


def test_missing_images_valid_key_defaults_to_valid_for_backward_compatibility():
    """A hand-built/older evidence bundle that predates this field must
    behave exactly as before: history[1] is used, nothing is skipped."""
    evidence = _surge_stuck_rollout_evidence(
        replicaset_history=[
            {"revision": 20, "created_at": 10_000.0 - 60, "replicas": 1,
             "ready_replicas": 0, "images": [PLACEHOLDER_IMAGE]},
            {"revision": 19, "created_at": 10_000.0 - 3600, "replicas": 1,
             "ready_replicas": 1, "images": [REAL_IMAGE_65]},
        ]
    )
    findings = _correlate(evidence)
    assert findings.previous_revision == 19
    assert findings.rollback_candidates_skipped == []
