"""
Kubernetes API wrapper — the ONLY module in Sentinel that can mutate the
cluster, and the only one that imports the `kubernetes` package.

Design constraints, all deliberate:

* **No shell, no kubectl.** There is no subprocess call anywhere in Sentinel.
  Everything is a typed API call through the official client. The container
  image does not even contain a kubectl binary (see the Dockerfile comment).
  This is what makes "the LLM cannot run commands" a structural property
  rather than a promise.

* **Three write operations, all of them a patch of one Deployment object:**
  restart (annotation), rollback (pod template), scale (spec.replicas).
  Nothing here can create, delete, or exec. The RBAC Role this needs is
  correspondingly tiny — see `REQUIRED_RBAC` at the bottom of this file,
  which is written to be copy-pasted into the Role definition.

* **The sync client, wrapped.** The official python client is synchronous
  (urllib3). Rather than fight it, every public method here is `async` and
  hands the blocking call to `asyncio.to_thread`, so a slow API server cannot
  block the FastAPI event loop and stall the Alertmanager webhook.

HONESTY NOTE: none of this has been executed against a real Kubernetes API
server yet. The object shapes and field names come from the documented
Deployment/ReplicaSet schema and mirror what `kubectl rollout restart` /
`kubectl rollout undo` do, but the first real run should be done with
DRY_RUN=true and the logs read carefully. The rollback path in particular
(reconstructing a pod template from a historical ReplicaSet) is the piece
most likely to need a tweak, because ReplicaSet templates carry
`pod-template-hash` labels that must be stripped before being written back
onto the Deployment.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

# Annotation kubectl itself uses for `kubectl rollout restart`. Writing this
# annotation into the pod *template* changes the template hash, which makes
# the Deployment controller roll out new pods. There is no "restart" verb in
# the Kubernetes API — this annotation trick IS the mechanism.
RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"

# Set by the Deployment controller on each ReplicaSet it creates. Parsing this
# is how `kubectl rollout undo` finds "the previous revision".
REVISION_ANNOTATION = "deployment.kubernetes.io/revision"

# Labels the Deployment controller injects into a ReplicaSet's own template.
# They MUST be stripped before writing that template back onto a Deployment,
# otherwise we pin the Deployment to a stale template hash and it will fight
# its own controller forever.
CONTROLLER_OWNED_LABELS = ("pod-template-hash",)


class KubernetesUnavailable(RuntimeError):
    """Raised when the in-cluster config/client could not be initialised.

    Distinct from an API error: this means Sentinel is not running in a pod
    with a ServiceAccount, which is a deployment mistake worth surfacing
    loudly rather than degrading silently.
    """


class KubernetesClient:
    def __init__(self, connection: Any = None) -> None:
        """`connection` is an
        `app.domain.environment.KubernetesConnectionConfig` (or None, which
        means "in-cluster", preserving the original behaviour for any caller
        that has not been migrated to the Environment model yet).

        Not type-hinted directly against that class to avoid a
        clients -> domain import for what is a one-field duck-typed read
        (`connection.mode`); keeps this module's only hard dependency the
        `kubernetes` package itself.
        """
        self._connection = connection
        self._apps: Any = None
        self._core: Any = None
        self._available = False
        self._init_error: str = ""

    # ---- lifecycle ------------------------------------------------------
    def initialise(self) -> bool:
        """Load cluster config and build the API stubs.

        Three modes, selected by `connection.mode` (default: in-cluster,
        unchanged from the original implementation):

        * ``in_cluster`` — the original behaviour. Only correct when Sentinel
          runs inside the same cluster as the workload.
        * ``kubeconfig`` — a base64-encoded kubeconfig YAML. This is how
          Sentinel reaches a REMOTE K3s/K8s API server as an external
          control plane; the token embedded in the kubeconfig should be
          scoped to the minimal Role in REQUIRED_RBAC below, not
          cluster-admin.
        * ``remote`` — explicit API server URL + bearer token + CA cert,
          for environments where handing over a full kubeconfig is
          undesirable.

        Imports are inside the method so that the unit tests (and anyone
        running Sentinel on a laptop) can import this module without the
        `kubernetes` package or a ServiceAccount present. The lifecycle code
        injects a fake client in tests; nothing here is import-time coupled.
        """
        conn = self._connection
        mode = getattr(conn, "mode", "in_cluster")
        try:
            from kubernetes import client as k8s_client  # noqa: PLC0415
            from kubernetes import config as k8s_config  # noqa: PLC0415

            if conn is None or mode == "in_cluster":
                k8s_config.load_incluster_config()
            elif mode == "kubeconfig":
                self._load_from_kubeconfig_b64(k8s_config, conn.kubeconfig_b64)
            elif mode == "remote":
                self._load_from_remote(k8s_client, conn)
            else:
                raise ValueError(f"unknown kubernetes connection mode {mode!r}")

            self._apps = k8s_client.AppsV1Api()
            self._core = k8s_client.CoreV1Api()
            self._available = True
            logger.info("kubernetes_client_ready", extra={"mode": mode})
        except Exception as exc:  # noqa: BLE001 - any failure means unavailable
            # Broad except on purpose: ImportError, ConfigException, and the
            # FileNotFoundError from a missing ServiceAccount token all mean
            # the same thing to us, and we want the reason in the log rather
            # than a traceback that kills startup.
            self._available = False
            self._init_error = str(exc)[:300]
            logger.warning(
                "kubernetes_client_unavailable",
                extra={"mode": mode, "error_detail": self._init_error},
            )
        return self._available

    @staticmethod
    def _load_from_kubeconfig_b64(k8s_config: Any, kubeconfig_b64: str | None) -> None:
        import base64  # noqa: PLC0415

        import yaml  # noqa: PLC0415 - transitive dep of `kubernetes`, no new package

        if not kubeconfig_b64:
            raise ValueError(
                "kubernetes_mode=kubeconfig but no kubeconfig was configured "
                "for this environment"
            )
        raw = base64.b64decode(kubeconfig_b64)
        config_dict = yaml.safe_load(raw)
        k8s_config.load_kube_config_from_dict(config_dict)

    @staticmethod
    def _load_from_remote(k8s_client: Any, conn: Any) -> None:
        import base64  # noqa: PLC0415
        import tempfile  # noqa: PLC0415

        if not conn.api_server or not conn.token:
            raise ValueError(
                "kubernetes_mode=remote requires both an API server URL and a "
                "bearer token"
            )
        configuration = k8s_client.Configuration()
        configuration.host = conn.api_server
        configuration.api_key = {"authorization": f"Bearer {conn.token}"}
        if conn.ca_cert_b64:
            ca_file = tempfile.NamedTemporaryFile(
                mode="wb", suffix=".crt", delete=False
            )
            ca_file.write(base64.b64decode(conn.ca_cert_b64))
            ca_file.close()
            configuration.ssl_ca_cert = ca_file.name
            configuration.verify_ssl = True
        else:
            # No CA supplied: only ever acceptable for a throwaway demo
            # environment reached over a trusted network (e.g. a VPN-only
            # K3s API server). Documented, not silently swallowed.
            configuration.verify_ssl = conn.verify_ssl
            if not conn.verify_ssl:
                logger.warning(
                    "kubernetes_tls_verification_disabled",
                    extra={"api_server": conn.api_server},
                )
        k8s_client.Configuration.set_default(configuration)

    @property
    def available(self) -> bool:
        return self._available

    @property
    def init_error(self) -> str:
        return self._init_error

    def _require(self) -> None:
        if not self._available:
            raise KubernetesUnavailable(self._init_error or "not initialised")

    # ---- reads ----------------------------------------------------------
    # RBAC: apps/deployments: get
    async def get_deployment(self, namespace: str, name: str) -> dict[str, Any] | None:
        """Return a trimmed dict view of a Deployment, or None if absent.

        We return a plain dict rather than the client's model object so that
        nothing downstream depends on the kubernetes package, which keeps the
        lifecycle modules unit-testable with hand-written fixtures.
        """
        self._require()

        def _call() -> Any:
            return self._apps.read_namespaced_deployment(name=name, namespace=namespace)

        try:
            dep = await asyncio.to_thread(_call)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "k8s_get_deployment_failed",
                extra={"namespace": namespace, "deployment": name,
                       "error_detail": str(exc)[:300]},
            )
            return None

        status = dep.status
        spec = dep.spec
        meta = dep.metadata
        return {
            "name": meta.name,
            "namespace": meta.namespace,
            "generation": meta.generation,
            "annotations": dict(meta.annotations or {}),
            "desired_replicas": spec.replicas,
            "available_replicas": status.available_replicas or 0,
            "ready_replicas": status.ready_replicas or 0,
            "updated_replicas": status.updated_replicas or 0,
            "unavailable_replicas": status.unavailable_replicas or 0,
            "observed_generation": status.observed_generation,
            "current_revision": (meta.annotations or {}).get(REVISION_ANNOTATION),
            "conditions": [
                {
                    "type": c.type,
                    "status": c.status,
                    "reason": c.reason,
                    "message": c.message,
                }
                for c in (status.conditions or [])
            ],
            "images": [
                c.image for c in (spec.template.spec.containers or []) if c.image
            ],
            # Container env, for Deep Investigation evidence and for
            # RemediationEngine's `before`-value capture (see
            # patch_deployment_env_var / remove_deployment_env_var above).
            # Read-only, and deliberately redacted at this single source
            # rather than at each caller: a name matching `_SENSITIVE_ENV_KEY`
            # never has its literal value surfaced to Sentinel's own
            # evidence, the LLM, or the GUI — there is no legitimate need for
            # a hostname/flag-style variable (which is what SET_ENV_VAR /
            # UNSET_ENV_VAR exist for) to ever require seeing a secret's
            # value, and `valueFrom`-sourced entries (secretKeyRef /
            # configMapKeyRef) are never resolved here at all — only the
            # reference's existence and kind is reported.
            "containers": [
                {
                    "name": c.name,
                    "env": _redacted_env(c.env or []),
                    # Added for the Deep Investigation typed remediation DSL
                    # (models/incident.py's NovelActionType /
                    # deep_investigation.apply_llm_response): a proposal to
                    # change one container's image, or to override its
                    # command/args, needs the CURRENT value of that exact
                    # field, per container, to validate against and to
                    # capture as `previous_*` for revert — the top-level
                    # `images` list above is container-order-only and not
                    # keyed by name, which is not enough once a Deployment
                    # can have more than one container carrying independent
                    # evidence. `command`/`args` are the container's own
                    # override (empty list if unset — never `None` vs `[]`
                    # ambiguity leaking through the API client), never
                    # resolved through a shell.
                    "image": c.image,
                    "command": list(c.command or []),
                    "args": list(c.args or []),
                }
                for c in (spec.template.spec.containers or [])
            ],
        }

    @staticmethod
    def _container_state_dict(cs: Any, is_init: bool) -> dict[str, Any]:
        """Shared shape for both `container_statuses` and
        `init_container_statuses` entries.

        Same fields either way so the correlation layer can treat "a
        container is crash-looping" identically regardless of which list it
        came from, and tell them apart with a single `is_init` flag rather
        than two differently-shaped dicts.
        """
        waiting = cs.state.waiting if cs.state else None
        terminated = cs.state.terminated if cs.state else None
        last_terminated = cs.last_state.terminated if cs.last_state else None
        return {
            "name": cs.name,
            "image": cs.image,
            "ready": cs.ready,
            "restart_count": cs.restart_count or 0,
            # waiting.reason is where CrashLoopBackOff and ImagePullBackOff
            # actually live — for an init container this is where
            # "Init:CrashLoopBackOff" shows up.
            "waiting_reason": waiting.reason if waiting else None,
            "terminated_reason": terminated.reason if terminated else None,
            "last_terminated_reason": (
                last_terminated.reason if last_terminated else None
            ),
            "exit_code": (
                terminated.exit_code
                if terminated is not None
                else (last_terminated.exit_code if last_terminated is not None else None)
            ),
            "started_at": (
                terminated.started_at.timestamp()
                if terminated is not None and terminated.started_at
                else None
            ),
            "finished_at": (
                terminated.finished_at.timestamp()
                if terminated is not None and terminated.finished_at
                else None
            ),
            "is_init": is_init,
        }

    # RBAC: core/pods: list
    async def list_pods(
        self, namespace: str, label_selector: str | None = None
    ) -> list[dict[str, Any]]:
        """Pods with readiness and restart counts.

        `restart_count` is the sum across containers. With no
        kube-state-metrics in this cluster, this API read is the *only* way
        Sentinel can see crash-loop behaviour — there is no
        `kube_pod_container_status_restarts_total` to query.

        `container_states` now includes BOTH the pod's regular containers
        AND its init containers (each tagged `is_init`). This matters
        because a pod stuck on a failing init container never starts its
        app container at all — `container_statuses` for the app container is
        empty in that case, so without `init_container_statuses` Sentinel
        would see nothing wrong with a pod that is actually
        `Init:CrashLoopBackOff`.
        """
        self._require()

        def _call() -> Any:
            return self._core.list_namespaced_pod(
                namespace=namespace, label_selector=label_selector
            )

        try:
            pods = await asyncio.to_thread(_call)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "k8s_list_pods_failed",
                extra={"namespace": namespace, "error_detail": str(exc)[:300]},
            )
            return []

        out: list[dict[str, Any]] = []
        for pod in pods.items:
            statuses = pod.status.container_statuses or []
            init_statuses = pod.status.init_container_statuses or []
            ready_conditions = [
                c for c in (pod.status.conditions or []) if c.type == "Ready"
            ]
            out.append(
                {
                    "name": pod.metadata.name,
                    "phase": pod.status.phase,
                    "ready": bool(ready_conditions and ready_conditions[0].status == "True"),
                    "restart_count": sum(cs.restart_count or 0 for cs in statuses),
                    "container_states": [
                        self._container_state_dict(cs, is_init=False) for cs in statuses
                    ]
                    + [
                        self._container_state_dict(cs, is_init=True)
                        for cs in init_statuses
                    ],
                    "start_time": (
                        pod.status.start_time.timestamp() if pod.status.start_time else None
                    ),
                }
            )
        return out

    # Hard ceiling on tail_lines, independent of whatever a caller asks for.
    # This is an evidence collector, not a log viewer: Sentinel only ever
    # needs enough lines to see the failure reason (a traceback, a
    # connection-refused line), not a full log dump, and an unbounded read
    # against a noisy container would be a needless cost/latency risk on
    # every investigation that touches it.
    MAX_LOG_TAIL_LINES = 200
    LOG_FETCH_TIMEOUT_SECONDS = 10.0

    # RBAC: core/pods/log: get
    async def get_container_logs(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        tail_lines: int = 100,
        previous: bool = False,
    ) -> dict[str, Any]:
        """Bounded tail of ONE explicitly named container's logs.

        This is the only log-retrieval path in KubernetesClient, and it is
        deliberately narrow: no shell, no `kubectl exec`, no "give me every
        container in the namespace" mode. The caller (investigation.py) must
        already know which pod and which container it wants — this method
        never discovers that on its own — which keeps "the LLM can read any
        log it likes" structurally impossible; only the deterministic
        investigation layer decides what gets fetched.

        `previous=True` reads the *last terminated* instance of the
        container rather than the current one. For a container stuck in
        CrashLoopBackOff, the running attempt is typically mid-backoff with
        no output yet, so the previous attempt's log is usually the one that
        actually explains the failure.

        Returns a structured result rather than raising, because a log fetch
        failing (RBAC, container not started yet, log already rotated) is
        evidence-collection noise, not an incident.
        """
        self._require()
        bounded_tail = max(1, min(tail_lines, self.MAX_LOG_TAIL_LINES))

        def _call() -> Any:
            return self._core.read_namespaced_pod_log(
                name=pod,
                namespace=namespace,
                container=container,
                tail_lines=bounded_tail,
                previous=previous,
                timestamps=True,
                _request_timeout=self.LOG_FETCH_TIMEOUT_SECONDS,
            )

        try:
            raw = await asyncio.to_thread(_call)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "k8s_get_container_logs_failed",
                extra={
                    "namespace": namespace,
                    "pod": pod,
                    "container": container,
                    "previous": previous,
                    "error_detail": str(exc)[:300],
                },
            )
            return {
                "pod": pod,
                "container": container,
                "previous": previous,
                "available": False,
                "error": str(exc)[:300],
                "lines": [],
            }

        lines = (raw or "").splitlines()[-bounded_tail:]
        return {
            "pod": pod,
            "container": container,
            "previous": previous,
            "available": True,
            "error": None,
            "lines": lines,
        }

    # RBAC: core/events: list
    async def list_events(
        self, namespace: str, since_seconds: float = 3600.0, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Namespace events, newest first, filtered to a time window.

        Events are the cheapest source of "what did Kubernetes itself do
        recently" — ScalingReplicaSet, Killing, Unhealthy, BackOff. We read
        the core/v1 Events API (not events.k8s.io/v1) because that is what
        the AppsV1/CoreV1 stubs give us and K3s serves both.
        """
        self._require()
        cutoff = time.time() - since_seconds

        def _call() -> Any:
            return self._core.list_namespaced_event(namespace=namespace, limit=500)

        try:
            events = await asyncio.to_thread(_call)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "k8s_list_events_failed",
                extra={"namespace": namespace, "error_detail": str(exc)[:300]},
            )
            return []

        out: list[dict[str, Any]] = []
        for event in events.items:
            ts = event.last_timestamp or event.first_timestamp or event.event_time
            epoch = ts.timestamp() if ts else 0.0
            if epoch and epoch < cutoff:
                continue
            out.append(
                {
                    "at": epoch,
                    "type": event.type,
                    "reason": event.reason,
                    "message": (event.message or "")[:500],
                    "object": (
                        f"{event.involved_object.kind}/{event.involved_object.name}"
                        if event.involved_object
                        else None
                    ),
                    "count": event.count or 1,
                }
            )
        out.sort(key=lambda e: e["at"], reverse=True)
        return out[:limit]

    # RBAC: apps/replicasets: list
    async def list_replicasets(
        self, namespace: str, deployment: str
    ) -> list[dict[str, Any]]:
        """Deployment history, newest revision first.

        This is what `kubectl rollout history` reads. Each ReplicaSet owned by
        the Deployment carries a `deployment.kubernetes.io/revision`
        annotation; the highest is the current one, and the next highest is
        the rollback target.

        We match by ownerReferences rather than by a name prefix, because a
        name prefix match would also pick up an unrelated Deployment called
        e.g. `citizen-service-canary`.
        """
        self._require()

        def _call() -> Any:
            return self._apps.list_namespaced_replica_set(namespace=namespace)

        try:
            replicasets = await asyncio.to_thread(_call)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "k8s_list_replicasets_failed",
                extra={"namespace": namespace, "deployment": deployment,
                       "error_detail": str(exc)[:300]},
            )
            return []

        out: list[dict[str, Any]] = []
        for rs in replicasets.items:
            owners = rs.metadata.owner_references or []
            if not any(
                o.kind == "Deployment" and o.name == deployment for o in owners
            ):
                continue
            revision_raw = (rs.metadata.annotations or {}).get(REVISION_ANNOTATION)
            try:
                revision = int(revision_raw) if revision_raw is not None else None
            except (TypeError, ValueError):
                revision = None
            init_images = [
                c.image for c in (rs.spec.template.spec.init_containers or []) if c.image
            ]
            container_images = [
                c.image for c in (rs.spec.template.spec.containers or []) if c.image
            ]
            out.append(
                {
                    "name": rs.metadata.name,
                    "revision": revision,
                    "created_at": (
                        rs.metadata.creation_timestamp.timestamp()
                        if rs.metadata.creation_timestamp
                        else None
                    ),
                    "replicas": rs.spec.replicas,
                    "ready_replicas": rs.status.ready_replicas or 0,
                    "images": container_images,
                    "init_images": init_images,
                    # Whether EVERY container and init container on this
                    # ReplicaSet has a plausible image reference — computed
                    # once, here, rather than re-derived by every caller
                    # that needs to pick a rollback candidate. This is what
                    # lets `correlate()` skip a numerically-previous
                    # revision that is itself broken (see
                    # `find_valid_rollback_candidate` in correlation.py)
                    # instead of blindly trusting "one revision back".
                    "images_valid": all(
                        _looks_like_a_valid_image_reference(img)
                        for img in (container_images + init_images)
                    ),
                    # Kept so the rollback path does not need a second API
                    # round-trip. This is the raw client model object, not a
                    # dict — the only place in this module that leaks a
                    # kubernetes type, and it never leaves this module (the
                    # Remediation Engine passes it straight back to
                    # patch_deployment_template).
                    "_template": rs.spec.template,
                }
            )
        out.sort(key=lambda r: (r["revision"] is None, -(r["revision"] or 0)))
        return out

    # ---- discovery (POST /environments/{id}/discover) -------------------
    # Deliberately namespace-scoped, not cluster-scoped: discovery only lists
    # within the namespace the environment was registered with, so it needs
    # no RBAC beyond what REQUIRED_RBAC already grants plus services:list.
    # See spec section 20 — "do not over-engineer it" is honoured by keeping
    # this to names + replica counts, no dependency graph.
    # RBAC: apps/deployments: list
    async def list_deployments(self, namespace: str) -> list[dict[str, Any]]:
        self._require()

        def _call() -> Any:
            return self._apps.list_namespaced_deployment(namespace=namespace)

        try:
            deployments = await asyncio.to_thread(_call)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "k8s_list_deployments_failed",
                extra={"namespace": namespace, "error_detail": str(exc)[:300]},
            )
            return []
        return [
            {
                "name": d.metadata.name,
                "desired_replicas": d.spec.replicas,
                "available_replicas": d.status.available_replicas or 0,
                "images": [
                    c.image for c in (d.spec.template.spec.containers or []) if c.image
                ],
            }
            for d in deployments.items
        ]

    # RBAC: core/services: list
    async def list_services(self, namespace: str) -> list[dict[str, Any]]:
        self._require()

        def _call() -> Any:
            return self._core.list_namespaced_service(namespace=namespace)

        try:
            services = await asyncio.to_thread(_call)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "k8s_list_services_failed",
                extra={"namespace": namespace, "error_detail": str(exc)[:300]},
            )
            return []
        return [
            {
                "name": svc.metadata.name,
                "cluster_ip": svc.spec.cluster_ip,
                "ports": [p.port for p in (svc.spec.ports or [])],
            }
            for svc in services.items
        ]

    # ---- writes (exactly three) -----------------------------------------
    # RBAC: apps/deployments: patch
    async def restart_deployment(self, namespace: str, name: str) -> dict[str, Any]:
        """Rollout restart, by stamping the restartedAt annotation.

        Identical to `kubectl rollout restart deploy/<name>`: patch the pod
        *template*'s annotations (not the Deployment's own metadata — that
        would change nothing about the pods). The changed template hash makes
        the Deployment controller create a new ReplicaSet and roll pods.

        A strategic-merge patch is used so we only touch that one annotation
        and cannot accidentally clobber annotations set by anything else.
        """
        self._require()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        body = {
            "spec": {
                "template": {
                    "metadata": {"annotations": {RESTART_ANNOTATION: stamp}}
                }
            }
        }

        def _call() -> Any:
            return self._apps.patch_namespaced_deployment(
                name=name, namespace=namespace, body=body
            )

        result = await asyncio.to_thread(_call)
        logger.info(
            "k8s_restart_patched",
            extra={"namespace": namespace, "deployment": name, "restarted_at": stamp},
        )
        return {"restarted_at": stamp, "generation": result.metadata.generation}

    # RBAC: apps/deployments: patch
    async def patch_deployment_template(
        self, namespace: str, name: str, template: Any, target_revision: int
    ) -> dict[str, Any]:
        """The `kubectl rollout undo` equivalent.

        kubectl's rollout undo does exactly this: take the pod template from
        the ReplicaSet at the target revision and write it back as the
        Deployment's template. The Deployment controller then notices the
        template changed, and (because that template hash already has a
        ReplicaSet) scales the old ReplicaSet back up. It creates a *new*
        revision number going forward — a rollback is a roll-forward to an
        old template, never a rewrite of history.

        The critical detail: strip `pod-template-hash` from the template's
        labels first. That label is controller-owned; leaving it in pins the
        Deployment's selector-matching to a stale hash and the rollout never
        completes. This is the part of the file most likely to need
        adjustment on first contact with a real cluster.
        """
        self._require()

        # `template` is a V1PodTemplateSpec from list_replicasets. Convert to
        # a plain dict via the client's sanitiser so we can edit labels
        # safely without mutating the object we were handed.
        template_dict = _to_dict(template)
        labels = ((template_dict.get("metadata") or {}).get("labels") or {})
        for owned in CONTROLLER_OWNED_LABELS:
            labels.pop(owned, None)
        template_dict.setdefault("metadata", {})["labels"] = labels

        # Defence-in-depth against InvalidImageName: whatever produced the
        # source ReplicaSet's template, this patch must never write a
        # container or init container with a missing/malformed image to the
        # Deployment. This does not fix a bad *source* template — it refuses
        # to apply one, converting a silent post-hoc Pod failure (which only
        # shows up minutes later as Init:InvalidImageName) into an explicit,
        # immediate, diagnosable RemediationResult failure naming exactly
        # which container and what it contained.
        _assert_valid_container_images(template_dict)

        # Record on the Deployment what we did and why. Anyone running
        # `kubectl describe deploy` after the fact sees Sentinel's fingerprint
        # instead of a mysterious template change.
        #
        # IMPORTANT: this annotation is NOT the same counter as
        # `deployment.kubernetes.io/revision`. `sentinel.sre/rolled-back-to`
        # records the *source* ReplicaSet/revision whose pod template was
        # restored — a historical, backward-looking number. The Deployment's
        # own `deployment.kubernetes.io/revision` keeps moving forward: this
        # very patch creates a brand-new revision (Kubernetes never reuses a
        # revision number — see this method's docstring), and any later
        # restart/rollback advances it further still. So it is expected and
        # correct for these two numbers to diverge, e.g. `rolled-back-to: 52`
        # sitting next to a Deployment now on revision 63 — they answer
        # different questions ("whose template did we restore" vs. "how many
        # template changes has this Deployment seen"), not the same one.
        #
        # ALSO IMPORTANT — and the reason `rolled-back-at` exists: this
        # annotation is only ever written HERE, i.e. only on a genuine
        # rollback. `restart_deployment()` never touches it. So it can sit
        # unchanged through any number of intervening restarts and is only
        # ever telling you "the last time an actual rollback ran, it
        # restored revision X" — not "revision X is what's running now" or
        # even "a rollback ran recently". Without a timestamp there is no
        # way to tell a rollback that just happened from one from days ago;
        # `rolled-back-at` makes that staleness checkable instead of
        # silently misleading.
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        template_dict["metadata"].setdefault("annotations", {})
        template_dict["metadata"]["annotations"]["sentinel.sre/rolled-back-to"] = str(
            target_revision
        )
        template_dict["metadata"]["annotations"]["sentinel.sre/rolled-back-at"] = stamp

        body = {"spec": {"template": template_dict}}

        def _call() -> Any:
            return self._apps.patch_namespaced_deployment(
                name=name, namespace=namespace, body=body
            )

        result = await asyncio.to_thread(_call)
        logger.info(
            "k8s_rollback_patched",
            extra={
                "namespace": namespace,
                "deployment": name,
                "target_revision": target_revision,
            },
        )
        return {
            "target_revision": target_revision,
            "generation": result.metadata.generation,
        }

    # RBAC: apps/deployments: patch
    async def scale_deployment(
        self, namespace: str, name: str, replicas: int
    ) -> dict[str, Any]:
        """Set spec.replicas.

        We patch the Deployment object directly rather than the
        `deployments/scale` subresource. Functionally equivalent, and it means
        the RBAC Role does not need a second resource entry — three write
        operations, one verb, one resource.

        There is no 0 guard here; the Policy Engine clamps to
        [MIN_REPLICAS, MAX_REPLICAS] with MIN_REPLICAS >= 1, and the
        Remediation Engine re-asserts it. Defence in depth, but the authority
        lives in policy.py.
        """
        self._require()
        body = {"spec": {"replicas": int(replicas)}}

        def _call() -> Any:
            return self._apps.patch_namespaced_deployment(
                name=name, namespace=namespace, body=body
            )

        result = await asyncio.to_thread(_call)
        logger.info(
            "k8s_scale_patched",
            extra={"namespace": namespace, "deployment": name, "replicas": replicas},
        )
        return {"replicas": replicas, "generation": result.metadata.generation}

    # RBAC: apps/deployments: patch (same verb/resource already granted above —
    # no new RBAC entry needed).
    #
    # Deep Investigation / novel typed remediation (see
    # models/incident.py's NovelActionType and lifecycle/remediation.py's
    # `execute_deep`). Two methods, both container-env-var writes, both
    # strategic-merge patches exactly as narrow as `restart_deployment`'s: the
    # patch body only ever names ONE container (by `name`, the corev1
    # `containers` list's merge key) and ONE env entry (by `name`, the corev1
    # `EnvVar` list's own merge key — see the Kubernetes API's
    # `x-kubernetes-patch-merge-key: name` / `x-kubernetes-patch-strategy:
    # merge` on `Container.env`). A strategic-merge patch shaped this way adds
    # or updates exactly that one variable and leaves every other container,
    # and every other env var on this one, untouched — there is still no
    # generic "patch this manifest" method, only two more narrowly-typed ones.
    async def patch_deployment_env_var(
        self, namespace: str, name: str, container: str, key: str, value: str
    ) -> dict[str, Any]:
        """Set (add or update) one environment variable on one container.

        This is the write side of SET_ENV_VAR. The caller (RemediationEngine)
        is responsible for having already captured the previous value (for
        revert / audit) — this method only ever applies the new one.
        """
        self._require()
        body = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"name": container, "env": [{"name": key, "value": str(value)}]}
                        ]
                    }
                }
            }
        }

        def _call() -> Any:
            return self._apps.patch_namespaced_deployment(
                name=name, namespace=namespace, body=body
            )

        result = await asyncio.to_thread(_call)
        logger.info(
            "k8s_env_var_set",
            extra={
                "namespace": namespace,
                "deployment": name,
                "container": container,
                "key": key,
            },
        )
        return {"key": key, "generation": result.metadata.generation}

    async def remove_deployment_env_var(
        self, namespace: str, name: str, container: str, key: str
    ) -> dict[str, Any]:
        """Remove one environment variable from one container.

        The write side of UNSET_ENV_VAR — and also how SET_ENV_VAR's own
        revert path undoes itself when the variable did not exist before
        Sentinel set it (see RemediationEngine._set_env_var's `before`
        capture). The `$patch: delete` directive is standard strategic-merge
        syntax for removing one entry from a merge-keyed list without
        touching the rest of it — this does not delete the container, only
        the one named env entry on it, and is a no-op (not an error) if the
        key was already absent.
        """
        self._require()
        body = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": container,
                                "env": [{"name": key, "$patch": "delete"}],
                            }
                        ]
                    }
                }
            }
        }

        def _call() -> Any:
            return self._apps.patch_namespaced_deployment(
                name=name, namespace=namespace, body=body
            )

        result = await asyncio.to_thread(_call)
        logger.info(
            "k8s_env_var_removed",
            extra={
                "namespace": namespace,
                "deployment": name,
                "container": container,
                "key": key,
            },
        )
        return {"key": key, "generation": result.metadata.generation}

    # ---- Deep Investigation / novel typed remediation: the four newer
    # DSL operations (see models/incident.py's NovelActionType). Same posture
    # as patch_deployment_env_var/remove_deployment_env_var above: each is a
    # strategic-merge patch naming exactly ONE container by its merge key
    # (`name`), touching exactly the one field this method exists for, and
    # nothing else on that container or any other one.
    async def patch_deployment_container_image(
        self, namespace: str, name: str, container: str, image: str
    ) -> dict[str, Any]:
        """Change one container's image — the write side of
        UPDATE_CONTAINER_IMAGE.

        Refuses (raises `InvalidContainerImage`, caught by RemediationEngine
        exactly like `InvalidRollbackTemplate` is) to send a syntactically
        invalid or placeholder image reference, using the SAME
        `_looks_like_a_valid_image_reference` check `patch_deployment_template`
        already applies to a rollback's source template. This is defence in
        depth: `deep_investigation.apply_llm_response` and
        `RemediationEngine.execute_deep` both already validate the image
        independently before this is ever called — this is the last gate
        immediately before the write, not the only one.
        """
        self._require()
        if not _looks_like_a_valid_image_reference(image):
            raise InvalidContainerImage(
                f"refusing to patch {namespace}/{name} container {container}: "
                f"{image!r} is not a plausible container image reference"
            )
        body = {
            "spec": {
                "template": {
                    "spec": {"containers": [{"name": container, "image": image}]}
                }
            }
        }

        def _call() -> Any:
            return self._apps.patch_namespaced_deployment(
                name=name, namespace=namespace, body=body
            )

        result = await asyncio.to_thread(_call)
        logger.info(
            "k8s_container_image_patched",
            extra={
                "namespace": namespace,
                "deployment": name,
                "container": container,
                "image": image,
            },
        )
        return {"image": image, "generation": result.metadata.generation}

    # Bounds on the size of a proposed command/args override — the same
    # "evidence collector, not a log viewer" posture as MAX_LOG_TAIL_LINES:
    # a real entrypoint override is a handful of short tokens, never a
    # sprawling script, and there is no `sh -c` anywhere in this codebase for
    # a long string to be interpreted by in the first place.
    MAX_ARGV_ITEMS = 20
    MAX_ARGV_ITEM_LEN = 512

    @classmethod
    def _assert_valid_argv(cls, argv: list[str] | None, *, field_name: str) -> None:
        if argv is None:
            return
        if not isinstance(argv, list) or len(argv) > cls.MAX_ARGV_ITEMS:
            raise InvalidContainerImage(  # reused: "refuse before the write, name what's wrong"
                f"refusing to patch: {field_name} must be a list of at most "
                f"{cls.MAX_ARGV_ITEMS} strings"
            )
        for item in argv:
            if not isinstance(item, str) or len(item) > cls.MAX_ARGV_ITEM_LEN:
                raise InvalidContainerImage(
                    f"refusing to patch: {field_name} contains a non-string or "
                    f"over-length ({cls.MAX_ARGV_ITEM_LEN}) entry"
                )

    async def patch_deployment_container_command(
        self, namespace: str, name: str, container: str, command: list[str] | None
    ) -> dict[str, Any]:
        """Override (or, with `command=None`, clear the override on) one
        container's entrypoint — the write side of UPDATE_CONTAINER_COMMAND.

        `command` is a plain argv list applied via `containers[].command`.
        Kubernetes replaces this field wholesale (it carries no
        `x-kubernetes-patch-merge-key`, unlike `env`), which is exactly the
        semantics wanted here: a command override IS the whole entrypoint,
        there is no per-token merge that would make sense. Never passed to a
        shell — there is no `sh -c` in this path, so a value containing
        shell metacharacters is just an inert argv element to the container
        runtime, never something a shell interprets.
        """
        self._require()
        self._assert_valid_argv(command, field_name="command")
        container_patch: dict[str, Any] = {"name": container}
        # An explicit `"command": None` in a strategic-merge patch body is
        # how you clear a scalar/list field back to unset — omitting the key
        # entirely would instead leave whatever was there untouched.
        container_patch["command"] = command
        body = {
            "spec": {"template": {"spec": {"containers": [container_patch]}}}
        }

        def _call() -> Any:
            return self._apps.patch_namespaced_deployment(
                name=name, namespace=namespace, body=body
            )

        result = await asyncio.to_thread(_call)
        logger.info(
            "k8s_container_command_patched",
            extra={"namespace": namespace, "deployment": name, "container": container},
        )
        return {"command": command, "generation": result.metadata.generation}

    async def patch_deployment_container_args(
        self, namespace: str, name: str, container: str, args: list[str] | None
    ) -> dict[str, Any]:
        """Override (or clear) one container's args — the write side of
        UPDATE_CONTAINER_ARGS. Same shape and same never-a-shell guarantee as
        `patch_deployment_container_command` above, for the `args` field."""
        self._require()
        self._assert_valid_argv(args, field_name="args")
        container_patch: dict[str, Any] = {"name": container, "args": args}
        body = {
            "spec": {"template": {"spec": {"containers": [container_patch]}}}
        }

        def _call() -> Any:
            return self._apps.patch_namespaced_deployment(
                name=name, namespace=namespace, body=body
            )

        result = await asyncio.to_thread(_call)
        logger.info(
            "k8s_container_args_patched",
            extra={"namespace": namespace, "deployment": name, "container": container},
        )
        return {"args": args, "generation": result.metadata.generation}


class InvalidContainerImage(ValueError):
    """Raised by `patch_deployment_container_image`/`_assert_valid_argv` —
    caught by RemediationEngine's generic `except Exception` and turned into
    a clean `DeepRemediationResult(succeeded=False, ...)`, the same pattern
    `InvalidRollbackTemplate` already established for the rollback path."""


class InvalidRollbackTemplate(ValueError):
    """Raised by `_assert_valid_container_images` — caught by
    RemediationEngine's generic `except Exception` and turned into a clean
    `RemediationResult(succeeded=False, ...)`, never an uncaught crash or a
    Deployment patch that goes on to produce InvalidImageName Pods."""


def _looks_like_a_valid_image_reference(image: Any) -> bool:
    """Cheap, deliberately permissive sanity check — this is a last-resort
    guard, not a full Docker reference-format validator (that regex is
    large and registries vary). It exists to catch two distinct failure
    modes:

    1. A Python object having been accidentally stringified
       (`str(container)`, `repr(...)`, `"{...}"`) instead of the real image
       string being read.
    2. A CI/CD or IaC templating placeholder that was never substituted —
       the live incident this guard was written for: a ReplicaSet whose
       image was literally the string
       "ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/.../citizen-service:PLACEHOLDER".
       That string is syntactically well-formed (no bad characters, not
       empty) — check (1) alone does not catch it. `ACCOUNT_ID` and
       `REGION` are checked as exact path segments, not substrings, so a
       real image is never falsely rejected: a genuine ECR host is
       `<12 digits>.dkr.ecr.<region>.amazonaws.com` — digits and a real
       lowercase region name (`eu-central-1`) never equal the literal
       uppercase token `ACCOUNT_ID`/`REGION`.
    """
    if not isinstance(image, str):
        return False
    image = image.strip()
    if not image:
        return False
    # Telltale signs of an object having been stringified instead of the
    # real `.image` field being read.
    if image in ("None", "null", "{}", "[]") or image.startswith(("{", "[", "<")):
        return False
    if any(ch.isspace() for ch in image):
        return False
    # Unsubstituted templating placeholders. Segment-based (split on the
    # separators an image reference actually uses), not a raw substring
    # search, so e.g. a hypothetical real repo path containing "region" as
    # part of a longer real word is not what is being matched here — these
    # are checked as whole path segments / tag values only.
    segments = re.split(r"[./:@]", image)
    if "ACCOUNT_ID" in segments or "REGION" in segments:
        return False
    if image.endswith(":PLACEHOLDER") or image.endswith("/PLACEHOLDER"):
        return False
    return True


# Public alias: `deep_investigation.py` (and its tests) validate a
# model-proposed image against this exact same guard used for rollback /
# initial-deployment validation, so a novel `UPDATE_CONTAINER_IMAGE`
# proposal can never be held to a weaker standard than the rest of the
# system. Exposed without the leading underscore because it is a
# deliberate, supported cross-module contract, not an accidental reach
# into a private helper.
looks_like_a_valid_image_reference = _looks_like_a_valid_image_reference


def _assert_valid_container_images(template_dict: dict[str, Any]) -> None:
    """Walk BOTH `containers` and `initContainers` in a (sanitised) pod
    template and refuse to proceed if any container's image is missing or
    obviously malformed — before this template is ever sent to the API
    server as a Deployment patch.
    """
    pod_spec = ((template_dict.get("spec") or {})) or {}
    bad: list[str] = []
    for field in ("containers", "initContainers"):
        for container in pod_spec.get(field) or []:
            name = container.get("name", "<unnamed>")
            image = container.get("image")
            if not _looks_like_a_valid_image_reference(image):
                bad.append(f"{field}[{name}]=image:{image!r}")
    if bad:
        raise InvalidRollbackTemplate(
            "refusing to patch Deployment: source template has invalid "
            "container image(s): " + "; ".join(bad)
        )


def _to_dict(obj: Any) -> dict[str, Any]:
    """Best-effort conversion of a kubernetes model object to a plain dict.

    The client ships `ApiClient.sanitize_for_serialization`, which is the
    correct way to do this (it handles the camelCase mapping that the API
    server expects — `serviceAccountName`, not `service_account_name`). If it
    is unavailable, or if we were handed a dict already, fall back gracefully
    rather than exploding mid-rollback.
    """
    if isinstance(obj, dict):
        return obj
    try:
        from kubernetes import client as k8s_client  # noqa: PLC0415

        sanitised = k8s_client.ApiClient().sanitize_for_serialization(obj)
        if isinstance(sanitised, dict):
            return sanitised
    except Exception as exc:  # noqa: BLE001
        logger.warning("k8s_template_serialisation_failed", extra={"error_detail": str(exc)[:200]})
    # Last resort: to_dict() gives snake_case keys, which the API server does
    # NOT accept. Returning it would produce a confusing 422 rather than a
    # silent misconfiguration, which is the lesser evil.
    return obj.to_dict() if hasattr(obj, "to_dict") else {}


# Substring match, case-insensitive, against an env var's NAME (never its
# value) — see get_deployment's own docstring note above. Deliberately broad
# (over-redacting a hostname-ish var named e.g. "API_KEY_ENDPOINT" is a far
# smaller cost than under-redacting a real secret).
#
# Public (not `_`-prefixed): also imported by lifecycle/deep_investigation.py,
# lifecycle/policy.py, and lifecycle/remediation.py, which each independently
# refuse a Deep Investigation proposal that names a matching key — at
# construction time, at the policy-eligibility check, and again immediately
# before the cluster write. A Deep Investigation proposal is LLM-authored and
# never rule-vetted the way the four known actions are, so this is the one
# additional boundary that action type needs: not just "which container" but
# "never a variable that looks like a secret", however confident or
# well-reasoned the model's proposal otherwise sounds. Reusing one constant
# across all four sites means tightening this list happens once, not four
# times that can drift.
SENSITIVE_ENV_KEY_MARKERS = (
    "password", "secret", "token", "key", "credential", "private", "auth",
)
# Backward-compatible alias for the one pre-existing internal reference.
_SENSITIVE_ENV_KEY_MARKERS = SENSITIVE_ENV_KEY_MARKERS


def _redacted_env(env_list: Any) -> list[dict[str, Any]]:
    """Trimmed, redacted view of one container's env list.

    `valueFrom`-sourced entries (secretKeyRef / configMapKeyRef / fieldRef)
    are never resolved — only which kind of reference it is. A plain literal
    value is passed through UNLESS its key name matches
    `_SENSITIVE_ENV_KEY_MARKERS`, in which case it is redacted exactly like a
    secret reference. This is the only place Sentinel reads container env
    vars from the cluster; every caller (evidence, Deep Investigation, the
    remediation `before`-capture) goes through this.
    """
    out: list[dict[str, Any]] = []
    for e in env_list:
        name = getattr(e, "name", None)
        if not name:
            continue
        sensitive = any(marker in name.lower() for marker in _SENSITIVE_ENV_KEY_MARKERS)
        value_from = getattr(e, "value_from", None)
        if value_from is not None:
            kind = (
                "secretKeyRef" if getattr(value_from, "secret_key_ref", None)
                else "configMapKeyRef" if getattr(value_from, "config_map_key_ref", None)
                else "fieldRef" if getattr(value_from, "field_ref", None)
                else "other"
            )
            out.append({"name": name, "value": None, "source": kind, "redacted": True})
            continue
        raw_value = getattr(e, "value", None)
        if sensitive and raw_value is not None:
            out.append({"name": name, "value": None, "source": "literal", "redacted": True})
        else:
            out.append({"name": name, "value": raw_value, "source": "literal", "redacted": False})
    return out


def find_previous_revision(
    replicasets: list[dict[str, Any]], target_revision: int | None = None
) -> dict[str, Any] | None:
    """Pick the rollback target from a revision-sorted ReplicaSet list.

    With `target_revision` given, return exactly that revision (or None) —
    an explicit, deliberate choice (the autonomous decision engine, or a
    human-authorized override that named a specific revision) is honoured
    as asked; `patch_deployment_template`'s `_assert_valid_container_images`
    call still refuses to apply it if its images are bad, so this can never
    by itself put a broken template on the cluster.

    Without a `target_revision`, this does NOT simply mean "the previous
    one" (plain `kubectl rollout undo` with no --to-revision). It returns
    the newest revision strictly older than the current one whose images
    are not known-invalid — the exact same rule
    `correlation._find_valid_rollback_candidate` applies when building the
    autonomous plan. Blindly returning `numbered[1]` regardless of its
    `images_valid` flag is precisely the citizen-service incident this
    guards against: a placeholder-image ReplicaSet sitting one revision
    back from the one that broke, "the previous revision" in name only. A
    candidate with no `images_valid` key (older evidence, a hand-built test
    fixture) is treated as valid, so this can only ever make Sentinel skip a
    target it previously would have blindly used, never reject one it used
    to accept.

    Returns None when there is nothing SAFE to roll back to. The Policy
    Engine turns that None into a hard denial rather than letting the
    Remediation Engine improvise.
    """
    numbered = [rs for rs in replicasets if rs.get("revision") is not None]
    numbered.sort(key=lambda r: r["revision"], reverse=True)
    if target_revision is not None:
        for rs in numbered:
            if rs["revision"] == target_revision:
                return rs
        return None
    for rs in numbered[1:]:
        if rs.get("images_valid", True):
            return rs
    return None


# Written here so the Role can be authored without reverse-engineering the
# call sites. This is the complete and minimal set — if you add a verb to
# this list you should be able to point at the method that needs it.
#
#   apiGroups: [""]      resources: ["pods"]        verbs: ["get", "list"]
#   apiGroups: [""]      resources: ["events"]      verbs: ["get", "list"]
#   apiGroups: ["apps"]  resources: ["deployments"] verbs: ["get", "list", "patch"]
#   apiGroups: ["apps"]  resources: ["replicasets"] verbs: ["get", "list"]
#
# Notably NOT needed: create, delete, update, watch, exec, pods/exec,
# pods/log, deployments/scale, secrets, configmaps, nodes. A Role scoped to
# namespace `citizen-portal` is sufficient; there is no cluster-scoped read
# anywhere in this module, so this should be a Role + RoleBinding, not a
# ClusterRole.
REQUIRED_RBAC: tuple[dict[str, Any], ...] = (
    {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
    {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
    {"apiGroups": [""], "resources": ["events"], "verbs": ["get", "list"]},
    {"apiGroups": [""], "resources": ["services"], "verbs": ["get", "list"]},
    {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["get", "list", "patch"]},
    {"apiGroups": ["apps"], "resources": ["replicasets"], "verbs": ["get", "list"]},
)
