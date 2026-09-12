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
                        {
                            "name": cs.name,
                            "ready": cs.ready,
                            # waiting.reason is where CrashLoopBackOff and
                            # ImagePullBackOff actually live.
                            "waiting_reason": (
                                cs.state.waiting.reason
                                if cs.state and cs.state.waiting
                                else None
                            ),
                            "terminated_reason": (
                                cs.state.terminated.reason
                                if cs.state and cs.state.terminated
                                else None
                            ),
                            "last_terminated_reason": (
                                cs.last_state.terminated.reason
                                if cs.last_state and cs.last_state.terminated
                                else None
                            ),
                        }
                        for cs in statuses
                    ],
                    "start_time": (
                        pod.status.start_time.timestamp() if pod.status.start_time else None
                    ),
                }
            )
        return out

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
                    "images": [
                        c.image for c in (rs.spec.template.spec.containers or []) if c.image
                    ],
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

        # Record on the Deployment what we did and why. Anyone running
        # `kubectl describe deploy` after the fact sees Sentinel's fingerprint
        # instead of a mysterious template change.
        template_dict["metadata"].setdefault("annotations", {})
        template_dict["metadata"]["annotations"]["sentinel.sre/rolled-back-to"] = str(
            target_revision
        )

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


def find_previous_revision(
    replicasets: list[dict[str, Any]], target_revision: int | None = None
) -> dict[str, Any] | None:
    """Pick the rollback target from a revision-sorted ReplicaSet list.

    With `target_revision` given, return exactly that revision (or None).
    Without it, return the second-highest revision — "the previous one",
    which is what `kubectl rollout undo` with no --to-revision does.

    Returns None when there is nothing to roll back to. The Policy Engine
    turns that None into a hard denial rather than letting the Remediation
    Engine improvise.
    """
    numbered = [rs for rs in replicasets if rs.get("revision") is not None]
    numbered.sort(key=lambda r: r["revision"], reverse=True)
    if target_revision is not None:
        for rs in numbered:
            if rs["revision"] == target_revision:
                return rs
        return None
    if len(numbered) < 2:
        return None
    return numbered[1]


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
    {"apiGroups": [""], "resources": ["events"], "verbs": ["get", "list"]},
    {"apiGroups": [""], "resources": ["services"], "verbs": ["get", "list"]},
    {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["get", "list", "patch"]},
    {"apiGroups": ["apps"], "resources": ["replicasets"], "verbs": ["get", "list"]},
)
