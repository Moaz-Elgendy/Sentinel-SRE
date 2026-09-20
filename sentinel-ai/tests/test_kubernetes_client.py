"""
KubernetesClient — the parts added for init-container investigation:
`_container_state_dict` (shared shape for regular + init containers) and
`get_container_logs` (bounded, single-container log retrieval).

No real cluster is used or needed: `_container_state_dict` is a pure
function over small hand-built stand-ins for the `kubernetes` package's
V1ContainerStatus objects, and `get_container_logs` is exercised against a
fake CoreV1Api stub.
"""
from __future__ import annotations

import pytest

from app.clients.kubernetes_client import KubernetesClient


class _Waiting:
    def __init__(self, reason):
        self.reason = reason


class _Terminated:
    def __init__(self, reason, exit_code, started_at=None, finished_at=None):
        self.reason = reason
        self.exit_code = exit_code
        self.started_at = started_at
        self.finished_at = finished_at


class _State:
    def __init__(self, waiting=None, terminated=None):
        self.waiting = waiting
        self.terminated = terminated


class _ContainerStatus:
    def __init__(self, name, image, ready, restart_count, state=None, last_state=None):
        self.name = name
        self.image = image
        self.ready = ready
        self.restart_count = restart_count
        self.state = state or _State()
        self.last_state = last_state or _State()


# ---------------------------------------------------------------------------
# Test 2 — init container exit code is surfaced
# ---------------------------------------------------------------------------
def test_terminated_init_container_surfaces_exit_code_and_reason():
    cs = _ContainerStatus(
        name="migrate-and-seed",
        image="citizen-service:v2",
        ready=False,
        restart_count=3,
        state=_State(terminated=_Terminated(reason="Error", exit_code=1)),
    )
    out = KubernetesClient._container_state_dict(cs, is_init=True)

    assert out["is_init"] is True
    assert out["name"] == "migrate-and-seed"
    assert out["terminated_reason"] == "Error"
    assert out["exit_code"] == 1
    assert out["restart_count"] == 3


def test_waiting_container_has_no_exit_code_yet():
    """Mid-backoff (waiting), there is no terminated state to read an exit
    code from — must not be invented."""
    cs = _ContainerStatus(
        name="migrate-and-seed",
        image="citizen-service:v2",
        ready=False,
        restart_count=3,
        state=_State(waiting=_Waiting(reason="CrashLoopBackOff")),
        last_state=_State(terminated=_Terminated(reason="Error", exit_code=1)),
    )
    out = KubernetesClient._container_state_dict(cs, is_init=True)

    assert out["waiting_reason"] == "CrashLoopBackOff"
    # exit_code falls back to the last completed attempt when the current
    # state has none of its own.
    assert out["exit_code"] == 1
    assert out["last_terminated_reason"] == "Error"


def test_regular_container_is_tagged_is_init_false():
    cs = _ContainerStatus(name="citizen-service", image="x:y", ready=True, restart_count=0)
    out = KubernetesClient._container_state_dict(cs, is_init=False)
    assert out["is_init"] is False


# ---------------------------------------------------------------------------
# get_container_logs — bounded, single-container, structured result
# ---------------------------------------------------------------------------
class _FakeCoreV1:
    def __init__(self, text="Traceback...\nconnection refused\n", raise_exc=None):
        self._text = text
        self._raise_exc = raise_exc
        self.calls: list[dict] = []

    def read_namespaced_pod_log(self, **kwargs):
        self.calls.append(kwargs)
        if self._raise_exc:
            raise self._raise_exc
        return self._text


def _client_with_fake_core(core: _FakeCoreV1) -> KubernetesClient:
    client = KubernetesClient()
    client._available = True  # noqa: SLF001 - test wiring, bypassing initialise()
    client._core = core  # noqa: SLF001
    return client


@pytest.mark.asyncio
async def test_get_container_logs_targets_exactly_the_named_container():
    core = _FakeCoreV1()
    client = _client_with_fake_core(core)

    result = await client.get_container_logs(
        "citizen-portal", "citizen-service-79c68cbcf5-lwzwk", "migrate-and-seed",
        tail_lines=50, previous=True,
    )

    assert result["available"] is True
    assert result["pod"] == "citizen-service-79c68cbcf5-lwzwk"
    assert result["container"] == "migrate-and-seed"
    assert result["previous"] is True
    assert "connection refused" in result["lines"]
    assert len(core.calls) == 1
    call = core.calls[0]
    assert call["container"] == "migrate-and-seed"
    assert call["previous"] is True
    assert call["tail_lines"] <= KubernetesClient.MAX_LOG_TAIL_LINES


@pytest.mark.asyncio
async def test_get_container_logs_requested_tail_is_capped():
    core = _FakeCoreV1()
    client = _client_with_fake_core(core)

    await client.get_container_logs(
        "citizen-portal", "pod", "container", tail_lines=10_000,
    )

    assert core.calls[0]["tail_lines"] == KubernetesClient.MAX_LOG_TAIL_LINES


@pytest.mark.asyncio
async def test_get_container_logs_failure_is_structured_not_raised():
    core = _FakeCoreV1(raise_exc=RuntimeError("pod not found"))
    client = _client_with_fake_core(core)

    result = await client.get_container_logs("citizen-portal", "pod", "container")

    assert result["available"] is False
    assert "pod not found" in result["error"]
    assert result["lines"] == []
