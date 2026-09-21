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


# ---------------------------------------------------------------------------
# patch_deployment_template — rollback must never write a missing/malformed
# container image, regardless of what produced the source template.
# ---------------------------------------------------------------------------
from app.clients.kubernetes_client import (  # noqa: E402
    InvalidRollbackTemplate,
    _looks_like_a_valid_image_reference,
)


class _FakeMeta:
    def __init__(self, generation=7):
        self.generation = generation


class _FakeDeploymentResult:
    def __init__(self, generation=7):
        self.metadata = _FakeMeta(generation)


class _FakeAppsV1:
    def __init__(self):
        self.calls: list[dict] = []

    def patch_namespaced_deployment(self, *, name, namespace, body):
        self.calls.append({"name": name, "namespace": namespace, "body": body})
        return _FakeDeploymentResult()


def _client_with_fake_apps(apps: _FakeAppsV1) -> KubernetesClient:
    client = KubernetesClient()
    client._available = True  # noqa: SLF001
    client._apps = apps  # noqa: SLF001
    return client


TAG_IMAGE = "123456789012.dkr.ecr.eu-west-1.amazonaws.com/sentinel-sre-demo/citizen-service:abc1234"
DIGEST_IMAGE = (
    "123456789012.dkr.ecr.eu-west-1.amazonaws.com/sentinel-sre-demo/citizen-service"
    "@sha256:" + "a" * 64
)


def _template(containers, init_containers=None):
    return {
        "metadata": {"labels": {"app": "citizen-service", "pod-template-hash": "old123"}},
        "spec": {
            "containers": containers,
            "initContainers": init_containers or [],
        },
    }


@pytest.mark.asyncio
async def test_rollback_preserves_container_and_init_container_image_exactly():
    apps = _FakeAppsV1()
    client = _client_with_fake_apps(apps)
    template = _template(
        containers=[{"name": "citizen-service", "image": TAG_IMAGE}],
        init_containers=[{"name": "migrate-and-seed", "image": TAG_IMAGE}],
    )

    await client.patch_deployment_template("citizen-portal", "citizen-service", template, 52)

    patched_spec = apps.calls[0]["body"]["spec"]["template"]["spec"]
    assert patched_spec["containers"][0]["image"] == TAG_IMAGE
    assert patched_spec["initContainers"][0]["image"] == TAG_IMAGE


@pytest.mark.asyncio
async def test_rollback_preserves_digest_pinned_images():
    apps = _FakeAppsV1()
    client = _client_with_fake_apps(apps)
    template = _template(
        containers=[{"name": "citizen-service", "image": DIGEST_IMAGE}],
        init_containers=[{"name": "migrate-and-seed", "image": DIGEST_IMAGE}],
    )

    await client.patch_deployment_template("citizen-portal", "citizen-service", template, 52)

    patched_spec = apps.calls[0]["body"]["spec"]["template"]["spec"]
    assert patched_spec["containers"][0]["image"] == DIGEST_IMAGE
    assert patched_spec["initContainers"][0]["image"] == DIGEST_IMAGE


@pytest.mark.asyncio
async def test_rollback_preserves_each_image_across_multiple_containers():
    apps = _FakeAppsV1()
    client = _client_with_fake_apps(apps)
    template = _template(
        containers=[
            {"name": "citizen-service", "image": TAG_IMAGE},
            {"name": "sidecar-proxy", "image": "envoyproxy/envoy:v1.29.0"},
        ],
        init_containers=[
            {"name": "migrate-and-seed", "image": TAG_IMAGE},
            {"name": "wait-for-db", "image": "busybox:1.36"},
        ],
    )

    await client.patch_deployment_template("citizen-portal", "citizen-service", template, 52)

    patched_spec = apps.calls[0]["body"]["spec"]["template"]["spec"]
    images_by_name = {
        c["name"]: c["image"]
        for c in patched_spec["containers"] + patched_spec["initContainers"]
    }
    assert images_by_name["citizen-service"] == TAG_IMAGE
    assert images_by_name["sidecar-proxy"] == "envoyproxy/envoy:v1.29.0"
    assert images_by_name["migrate-and-seed"] == TAG_IMAGE
    assert images_by_name["wait-for-db"] == "busybox:1.36"


@pytest.mark.asyncio
async def test_rollback_refuses_a_template_with_a_missing_container_image():
    """The core regression guard: a source template with no image must never
    reach patch_namespaced_deployment — this is what would eventually surface
    as InvalidImageName Pods."""
    apps = _FakeAppsV1()
    client = _client_with_fake_apps(apps)
    template = _template(containers=[{"name": "citizen-service", "image": None}])

    with pytest.raises(InvalidRollbackTemplate):
        await client.patch_deployment_template("citizen-portal", "citizen-service", template, 52)
    assert apps.calls == []  # never reached the API server


@pytest.mark.asyncio
async def test_rollback_refuses_a_stringified_object_as_an_image():
    """Guards against exactly the class of bug the doc called out:
    str(container)/repr(container)/None-as-string leaking into .image."""
    apps = _FakeAppsV1()
    client = _client_with_fake_apps(apps)
    template = _template(containers=[{"name": "citizen-service", "image": "None"}])

    with pytest.raises(InvalidRollbackTemplate):
        await client.patch_deployment_template("citizen-portal", "citizen-service", template, 52)
    assert apps.calls == []


@pytest.mark.asyncio
async def test_rollback_refuses_when_only_the_init_container_image_is_bad():
    """Preserving containers[].image while forgetting initContainers[].image
    is exactly the bug class called out — this proves BOTH lists are checked."""
    apps = _FakeAppsV1()
    client = _client_with_fake_apps(apps)
    template = _template(
        containers=[{"name": "citizen-service", "image": TAG_IMAGE}],
        init_containers=[{"name": "migrate-and-seed", "image": ""}],
    )

    with pytest.raises(InvalidRollbackTemplate):
        await client.patch_deployment_template("citizen-portal", "citizen-service", template, 52)
    assert apps.calls == []


@pytest.mark.parametrize(
    "image,expected",
    [
        (TAG_IMAGE, True),
        (DIGEST_IMAGE, True),
        ("busybox:1.36", True),
        (None, False),
        ("", False),
        ("   ", False),
        ("None", False),
        ("{}", False),
        ("<V1Container object>", False),
        ("has a space:latest", False),
        # The live citizen-service/frontend incident: an unsubstituted
        # CI/CD templating placeholder. Syntactically well-formed (no bad
        # characters, not empty) — only the placeholder-segment check
        # catches it.
        ("ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/sentinel-sre-demo/citizen-service:PLACEHOLDER", False),
        ("890608336467.dkr.ecr.eu-central-1.amazonaws.com/sentinel-sre-demo/frontend:PLACEHOLDER", False),
        # A REAL ECR image must never be rejected: numeric account id, real
        # lowercase region — neither literally equals "ACCOUNT_ID"/"REGION".
        (
            "890608336467.dkr.ecr.eu-central-1.amazonaws.com/sentinel-sre-demo/"
            "citizen-service:6e36eedd0da47e4a24f0aa5a1c534ce6f4954a84",
            True,
        ),
    ],
)
def test_looks_like_a_valid_image_reference(image, expected):
    assert _looks_like_a_valid_image_reference(image) is expected


# ---------------------------------------------------------------------------
# list_replicasets — images_valid must see BOTH containers and
# initContainers, and must not be fooled by a well-formed-looking
# placeholder host/tag.
# ---------------------------------------------------------------------------
class _Owner:
    def __init__(self, kind, name):
        self.kind = kind
        self.name = name


class _RSMeta:
    def __init__(self, name, revision, owner_name, creation_timestamp=None):
        self.name = name
        self.annotations = {"deployment.kubernetes.io/revision": str(revision)}
        self.owner_references = [_Owner("Deployment", owner_name)]
        self.creation_timestamp = creation_timestamp


class _RSStatus:
    def __init__(self, ready_replicas=0):
        self.ready_replicas = ready_replicas


class _PodTemplateSpec:
    def __init__(self, containers, init_containers=None):
        self.containers = containers
        self.init_containers = init_containers or []


class _PodTemplate:
    def __init__(self, containers, init_containers=None):
        self.spec = _PodTemplateSpec(containers, init_containers)


class _RSSpec:
    def __init__(self, replicas, containers, init_containers=None):
        self.replicas = replicas
        self.template = _PodTemplate(containers, init_containers)


class _FakeReplicaSet:
    def __init__(
        self, name, revision, owner_name, containers, init_containers=None,
        replicas=1, ready_replicas=0,
    ):
        self.metadata = _RSMeta(name, revision, owner_name)
        self.spec = _RSSpec(replicas, containers, init_containers)
        self.status = _RSStatus(ready_replicas)


class _FakeAppsV1ForReplicaSets:
    def __init__(self, items):
        self._items = items

    def list_namespaced_replica_set(self, namespace):
        class _List:
            def __init__(self, items):
                self.items = items

        return _List(self._items)


def _client_with_fake_replicasets(items) -> KubernetesClient:
    client = KubernetesClient()
    client._available = True  # noqa: SLF001
    client._apps = _FakeAppsV1ForReplicaSets(items)  # noqa: SLF001
    return client


@pytest.mark.asyncio
async def test_list_replicasets_marks_a_real_ecr_image_valid():
    class _C:
        def __init__(self, image):
            self.image = image

    rs = _FakeReplicaSet(
        "citizen-service-5654c9bc4c", 65, "citizen-service",
        containers=[_C(TAG_IMAGE)], init_containers=[_C(TAG_IMAGE)],
    )

    client = _client_with_fake_replicasets([rs])
    out = await client.list_replicasets("citizen-portal", "citizen-service")

    assert len(out) == 1
    assert out[0]["images_valid"] is True
    assert out[0]["init_images"] == [TAG_IMAGE]


@pytest.mark.asyncio
async def test_list_replicasets_flags_placeholder_container_image_as_invalid():
    class _C:
        def __init__(self, image):
            self.image = image

    rs = _FakeReplicaSet(
        "citizen-service-5f5f6754bf", 66, "citizen-service",
        containers=[_C(
            "ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/sentinel-sre-demo/citizen-service:PLACEHOLDER"
        )],
        init_containers=[_C(
            "ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/sentinel-sre-demo/citizen-service:PLACEHOLDER"
        )],
    )

    client = _client_with_fake_replicasets([rs])
    out = await client.list_replicasets("citizen-portal", "citizen-service")

    assert out[0]["images_valid"] is False


@pytest.mark.asyncio
async def test_list_replicasets_flags_invalid_when_only_init_container_is_bad():
    """The `frontend`/`citizen-service` dual-container concern: preserving
    (or here, validating) `containers[]` while forgetting `initContainers[]`
    is exactly the bug class this guards against."""
    class _C:
        def __init__(self, image):
            self.image = image

    rs = _FakeReplicaSet(
        "citizen-service-abc", 66, "citizen-service",
        containers=[_C(TAG_IMAGE)],
        init_containers=[_C("ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/x/y:PLACEHOLDER")],
    )

    client = _client_with_fake_replicasets([rs])
    out = await client.list_replicasets("citizen-portal", "citizen-service")

    assert out[0]["images_valid"] is False


@pytest.mark.asyncio
async def test_list_replicasets_filters_by_owning_deployment():
    class _C:
        def __init__(self, image):
            self.image = image

    owned = _FakeReplicaSet("citizen-service-a", 65, "citizen-service", containers=[_C(TAG_IMAGE)])
    other = _FakeReplicaSet("frontend-a", 43, "frontend", containers=[_C(TAG_IMAGE)])

    client = _client_with_fake_replicasets([owned, other])
    out = await client.list_replicasets("citizen-portal", "citizen-service")

    assert [r["name"] for r in out] == ["citizen-service-a"]
