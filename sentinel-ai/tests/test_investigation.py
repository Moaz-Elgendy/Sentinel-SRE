"""
investigate() — GitHub commit correlation wiring specifically.

The rest of this phase (metrics/logs/k8s collection) already has behaviour
implied by rca.py's tests exercising its output shape; this file is scoped
to the one thing added here: pulling a commit SHA out of the newest
ReplicaSet's image tag and folding GitHubClient.get_commit() into Evidence,
without ever turning a GitHub problem into an investigation failure.
"""
from __future__ import annotations

import pytest

from app.lifecycle.investigation import (
    INIT_CONTAINER_LOG_TAIL_LINES,
    _extract_sha_from_image,
    _find_failing_init_container,
    investigate,
)
from app.models.incident import Evidence, Incident, Severity
from tests.conftest import FakeGitHub, FakeKubernetes


# ---------------------------------------------------------------------------
# _extract_sha_from_image — pure function, the cheapest thing to pin exactly
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "image,expected",
    [
        (
            "123456789012.dkr.ecr.eu-west-1.amazonaws.com/sentinel-sre-demo/"
            "citizen-service:9f3c1a8b2d4e5f60718293a4b5c6d7e8f9012345",
            "9f3c1a8b2d4e5f60718293a4b5c6d7e8f9012345",
        ),
        ("citizen-service:latest", "latest"),
        ("citizen-service", None),  # no tag at all
        ("", None),
    ],
)
def test_extract_sha_from_image(image, expected):
    assert _extract_sha_from_image(image) == expected


# ---------------------------------------------------------------------------
# Fakes with no network/cluster (matches the rest of the suite's contract)
# ---------------------------------------------------------------------------
class FakePrometheusAllNone:
    """Every collector returns an empty/None result — this file only cares
    about the github wiring, not the metric collection already covered
    elsewhere."""

    async def error_rate(self, app): return None
    async def error_count(self, app): return None
    async def request_rate(self, app): return None
    async def p95_latency(self, app): return None
    async def cpu_cores(self, app): return None
    async def memory_bytes(self, app): return None
    async def memory_growth_bytes(self, app): return None
    async def up(self, app): return None
    async def chaos_state(self, app): return {}
    async def chaos_injections(self, app): return {}
    async def notification_deliveries(self): return {}
    async def notification_dispatch_failures(self): return None


class FakeLokiEmpty:
    async def recent_errors(self, app, namespace, start, now): return []
    async def access_log_errors(self, app, namespace, start, now): return []


SHA = "9f3c1a8b2d4e5f60718293a4b5c6d7e8f9012345"
IMAGE = f"123456789012.dkr.ecr.eu-west-1.amazonaws.com/sentinel-sre-demo/citizen-service:{SHA}"


def _incident():
    return Incident(
        id="INC-TEST-GH-1",
        fingerprint="fp1",
        alertname="HighHTTPErrorRate",
        severity=Severity.CRITICAL,
        app="citizen-service",
        namespace="citizen-portal",
        evidence=Evidence(),
    )


@pytest.mark.asyncio
async def test_investigate_populates_deploy_commit_when_github_enabled():
    commit = {
        "sha": SHA[:12],
        "message": "widen db connection pool timeout",
        "changed_files": ["app/db.py"],
        "changed_file_count": 1,
        "pull_request": None,
    }
    k8s = FakeKubernetes(
        deployment={"available_replicas": 1, "replicas": 1},
        replicasets=[{"revision": 3, "created_at": 0.0, "images": [IMAGE]}],
    )
    github = FakeGitHub(enabled=True, commit=commit)

    evidence = await investigate(
        incident=_incident(),
        prom=FakePrometheusAllNone(),
        loki=FakeLokiEmpty(),
        k8s=k8s,
        github=github,
    )

    assert evidence.deploy_commit == commit
    assert github.lookups == [SHA]


@pytest.mark.asyncio
async def test_investigate_skips_github_when_not_configured():
    k8s = FakeKubernetes(
        deployment={"available_replicas": 1, "replicas": 1},
        replicasets=[{"revision": 3, "created_at": 0.0, "images": [IMAGE]}],
    )
    github = FakeGitHub(enabled=False)

    evidence = await investigate(
        incident=_incident(),
        prom=FakePrometheusAllNone(),
        loki=FakeLokiEmpty(),
        k8s=k8s,
        github=github,
    )

    assert evidence.deploy_commit is None
    assert github.lookups == []  # never even attempted


@pytest.mark.asyncio
async def test_investigate_skips_github_when_client_is_none():
    """github=None (the default) must behave identically to a disabled
    client — this is what happens whenever this function is called from
    anywhere that doesn't pass one, including any future caller."""
    k8s = FakeKubernetes(
        deployment={"available_replicas": 1, "replicas": 1},
        replicasets=[{"revision": 3, "created_at": 0.0, "images": [IMAGE]}],
    )

    evidence = await investigate(
        incident=_incident(),
        prom=FakePrometheusAllNone(),
        loki=FakeLokiEmpty(),
        k8s=k8s,
        github=None,
    )

    assert evidence.deploy_commit is None


@pytest.mark.asyncio
async def test_investigate_records_a_gap_when_commit_lookup_finds_nothing():
    """A configured-but-empty result (e.g. SHA not in the repo) must show up
    in evidence.errors, so the incident document can say WHY there's no
    commit evidence rather than looking like the collector never ran."""
    k8s = FakeKubernetes(
        deployment={"available_replicas": 1, "replicas": 1},
        replicasets=[{"revision": 3, "created_at": 0.0, "images": [IMAGE]}],
    )
    github = FakeGitHub(enabled=True, commit=None)  # simulates a 404 from GitHub

    evidence = await investigate(
        incident=_incident(),
        prom=FakePrometheusAllNone(),
        loki=FakeLokiEmpty(),
        k8s=k8s,
        github=github,
    )

    assert evidence.deploy_commit is None
    assert any("github" in e.lower() for e in evidence.errors)


@pytest.mark.asyncio
async def test_investigate_has_no_replicaset_history_skips_github_entirely():
    """No deployment history at all (e.g. brand-new/unknown target) must not
    attempt a lookup with no SHA to look up."""
    k8s = FakeKubernetes(
        deployment={"available_replicas": 1, "replicas": 1},
        replicasets=[],
    )
    github = FakeGitHub(enabled=True, commit={"sha": "irrelevant"})

    evidence = await investigate(
        incident=_incident(),
        prom=FakePrometheusAllNone(),
        loki=FakeLokiEmpty(),
        k8s=k8s,
        github=github,
    )

    assert evidence.deploy_commit is None
    assert github.lookups == []


# ---------------------------------------------------------------------------
# Test 3 — targeted, bounded init-container log retrieval
# ---------------------------------------------------------------------------
FAILING_POD = {
    "name": "citizen-service-79c68cbcf5-lwzwk",
    "phase": "Pending",
    "ready": False,
    "restart_count": 3,
    "container_states": [
        {
            "name": "citizen-service",
            "image": IMAGE,
            "ready": False,
            "restart_count": 0,
            "waiting_reason": None,
            "terminated_reason": None,
            "last_terminated_reason": None,
            "exit_code": None,
            "started_at": None,
            "finished_at": None,
            "is_init": False,
        },
        {
            "name": "migrate-and-seed",
            "image": IMAGE,
            "ready": False,
            "restart_count": 3,
            "waiting_reason": "CrashLoopBackOff",
            "terminated_reason": None,
            "last_terminated_reason": "Error",
            "exit_code": 1,
            "started_at": None,
            "finished_at": None,
            "is_init": True,
        },
    ],
}

HEALTHY_POD = {
    "name": "citizen-service-69556bc7fb-abcde",
    "phase": "Running",
    "ready": True,
    "restart_count": 0,
    "container_states": [
        {
            "name": "citizen-service",
            "image": IMAGE,
            "ready": True,
            "restart_count": 0,
            "waiting_reason": None,
            "terminated_reason": None,
            "last_terminated_reason": None,
            "exit_code": None,
            "started_at": None,
            "finished_at": None,
            "is_init": False,
        },
    ],
}


def test_find_failing_init_container_targets_only_the_failing_one():
    result = _find_failing_init_container([HEALTHY_POD, FAILING_POD])
    assert result == ("citizen-service-79c68cbcf5-lwzwk", "migrate-and-seed", True)


def test_find_failing_init_container_returns_none_when_all_healthy():
    assert _find_failing_init_container([HEALTHY_POD]) is None


@pytest.mark.asyncio
async def test_investigate_fetches_logs_only_for_the_failing_init_container():
    k8s = FakeKubernetes(
        deployment={"available_replicas": 1, "replicas": 1},
        pods=[HEALTHY_POD, FAILING_POD],
        replicasets=[{"revision": 3, "created_at": 0.0, "images": [IMAGE]}],
    )

    evidence = await investigate(
        incident=_incident(),
        prom=FakePrometheusAllNone(),
        loki=FakeLokiEmpty(),
        k8s=k8s,
        github=None,
    )

    # Exactly one targeted fetch — never a sweep of every container in
    # every pod.
    assert k8s.log_calls == [
        ("citizen-portal", "citizen-service-79c68cbcf5-lwzwk", "migrate-and-seed",
         INIT_CONTAINER_LOG_TAIL_LINES, True)
    ]
    assert len(evidence.init_container_logs) == 1
    assert evidence.init_container_logs[0]["container"] == "migrate-and-seed"
    assert INIT_CONTAINER_LOG_TAIL_LINES <= 200  # matches the client-side cap


@pytest.mark.asyncio
async def test_investigate_does_not_fetch_logs_when_nothing_is_failing():
    """The bounded/targeted contract: a normal incident with no init-container
    problem must not trigger any log fetch at all."""
    k8s = FakeKubernetes(
        deployment={"available_replicas": 1, "replicas": 1},
        pods=[HEALTHY_POD],
        replicasets=[{"revision": 3, "created_at": 0.0, "images": [IMAGE]}],
    )

    evidence = await investigate(
        incident=_incident(),
        prom=FakePrometheusAllNone(),
        loki=FakeLokiEmpty(),
        k8s=k8s,
        github=None,
    )

    assert k8s.log_calls == []
    assert evidence.init_container_logs == []


@pytest.mark.asyncio
async def test_investigate_records_a_gap_when_log_fetch_fails():
    k8s = FakeKubernetes(
        deployment={"available_replicas": 1, "replicas": 1},
        pods=[FAILING_POD],
        replicasets=[{"revision": 3, "created_at": 0.0, "images": [IMAGE]}],
        container_logs={
            ("citizen-service-79c68cbcf5-lwzwk", "migrate-and-seed", True): {
                "pod": "citizen-service-79c68cbcf5-lwzwk",
                "container": "migrate-and-seed",
                "previous": True,
                "available": False,
                "error": "container not found",
                "lines": [],
            }
        },
    )

    evidence = await investigate(
        incident=_incident(),
        prom=FakePrometheusAllNone(),
        loki=FakeLokiEmpty(),
        k8s=k8s,
        github=None,
    )

    assert evidence.init_container_logs[0]["available"] is False
    # A failed fetch is recorded on the evidence bundle, not raised — same
    # fail-soft contract as every other collector in this phase.
    assert evidence.init_container_logs[0]["available"] is False
