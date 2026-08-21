"""
AUTONOMOUS EXECUTION — the Remediation Engine.

    LLM -> Decision Engine -> Policy Engine -> **Remediation Engine** -> K8s

This is the last gate. It is the only module in Sentinel that calls a write
method on the Kubernetes client or posts to the chaos control plane, and it
refuses to do either without a `PolicyVerdict` that says `allowed=True`.

### Why it re-checks the allow-lists

The Policy Engine already validated the namespace and deployment. This module
validates them *again*, from its own frozen copy, before every write. That is
not redundancy for its own sake:

  * It makes the Remediation Engine safe in isolation. Someone adding a new
    call site (a manual-trigger endpoint, a test harness, a future scheduled
    job) cannot bypass the allow-list by forgetting to route through policy.
  * The verdict is a data object. If a bug ever produced a verdict whose
    `adjusted_params` pointed somewhere else, the verdict alone would
    authorise it. Re-deriving the target from the params and re-checking it
    closes that gap.
  * It is the difference between "we check permissions in the right place"
    and "there is no code path that writes to an unapproved target". Only the
    second one is a guarantee.

### What is deliberately absent

No `subprocess`. No `os.system`. No `eval`. No kubectl. No generic
"apply this manifest" method. No way to pass a free-text argument into a
Kubernetes call. The four actions map to three specific typed patch bodies
and one HTTP POST, and that is the entire blast radius.

HONESTY NOTE: none of the write paths here have been executed against a real
Kubernetes API server. Run the first real incident with DRY_RUN=true and read
the logged patch bodies before trusting it.
"""
from __future__ import annotations

import logging
import time

from app.clients.chaos_client import ChaosClient
from app.clients.kubernetes_client import (
    KubernetesClient,
    KubernetesUnavailable,
    find_previous_revision,
)
from app.core.metrics import observe_remediation
from app.models.incident import (
    ActionParams,
    ActionPlan,
    PolicyVerdict,
    RemediationAction,
    RemediationResult,
)

logger = logging.getLogger(__name__)


class RemediationRefused(RuntimeError):
    """Raised when the engine refuses to execute.

    Distinct from a failed execution: refusal means the request never should
    have reached here, which is a bug or an attack, and it must be loud.
    """


class RemediationEngine:
    def __init__(
        self,
        k8s: KubernetesClient,
        chaos: ChaosClient,
        allowed_namespaces: frozenset[str],
        allowed_deployments: frozenset[str],
        denied_deployments: frozenset[str],
        denied_namespaces: frozenset[str],
        min_replicas: int,
        max_replicas: int,
        base_url_resolver=None,
        dry_run: bool = False,
    ) -> None:
        self.k8s = k8s
        self.chaos = chaos
        # Frozen sets, captured at construction. Not read from `settings` at
        # call time, so a mutated global cannot widen this engine's authority
        # mid-incident.
        self.allowed_namespaces = allowed_namespaces
        self.allowed_deployments = allowed_deployments
        self.denied_deployments = denied_deployments
        self.denied_namespaces = denied_namespaces
        self.min_replicas = min_replicas
        self.max_replicas = max_replicas
        # Callable (service_name) -> base_url|None. Injected so the engine
        # cannot construct an arbitrary outbound URL: a target that is not in
        # the resolver's table simply has no reachable chaos API.
        self.base_url_resolver = base_url_resolver or (lambda _name: None)
        self.dry_run = dry_run

    # ---- the independent gate -------------------------------------------
    def assert_target_permitted(self, params: ActionParams) -> None:
        """Re-validate the target. Raises RemediationRefused on any doubt.

        Deny-lists first, then allow-lists, then presence. Same order as the
        Policy Engine, for the same reason: the most absolute rule should
        produce the error message.
        """
        namespace = params.namespace
        deployment = params.deployment

        if not namespace or not deployment:
            raise RemediationRefused(
                "remediation refused: action has no namespace/deployment target"
            )
        if namespace in self.denied_namespaces:
            raise RemediationRefused(
                f"remediation refused: namespace {namespace} is permanently denied"
            )
        if deployment in self.denied_deployments:
            raise RemediationRefused(
                f"remediation refused: {deployment} is permanently denied "
                "(stateful database workload)"
            )
        if namespace not in self.allowed_namespaces:
            raise RemediationRefused(
                f"remediation refused: namespace {namespace} is not allow-listed"
            )
        if deployment not in self.allowed_deployments:
            raise RemediationRefused(
                f"remediation refused: deployment {deployment} is not allow-listed"
            )

    def _assert_authorised(self, plan: ActionPlan, verdict: PolicyVerdict) -> ActionParams:
        """Check the verdict and return the params that will actually be used."""
        if verdict is None or not verdict.allowed:
            raise RemediationRefused(
                f"remediation refused: no policy authorisation for {plan.action.value}"
            )
        if verdict.action is not plan.action:
            # A verdict for a different action is not an authorisation for
            # this one. Catches a mis-wired orchestrator loop.
            raise RemediationRefused(
                f"remediation refused: verdict authorises {verdict.action.value} "
                f"but the plan is {plan.action.value}"
            )
        params = verdict.adjusted_params or plan.params
        self.assert_target_permitted(params)
        return params

    # ---- execution ------------------------------------------------------
    async def execute(
        self, plan: ActionPlan, verdict: PolicyVerdict
    ) -> RemediationResult:
        """Perform the authorised action. Never raises for operational errors.

        Operational failures (API server rejected the patch, chaos endpoint
        404) are returned as `succeeded=False` so the orchestrator can
        re-investigate and try the next candidate. Only a *refusal* raises,
        because a refusal means something upstream is broken.
        """
        params = self._assert_authorised(plan, verdict)
        started = time.time()

        if self.dry_run:
            # DRY_RUN logs exactly what would have happened, including the
            # resolved parameters after any policy clamping. This is the mode
            # to use for the first real run against a live cluster.
            logger.info(
                "remediation_dry_run",
                extra={
                    "action": plan.action.value,
                    "namespace": params.namespace,
                    "deployment": params.deployment,
                    "replicas": params.replicas,
                    "target_revision": params.target_revision,
                },
            )
            observe_remediation(plan.action.value, "dry_run", 0.0)
            return RemediationResult(
                action=plan.action,
                params=params,
                succeeded=True,
                dry_run=True,
                detail="DRY_RUN=true: action was authorised and fully resolved but "
                "not applied to the cluster",
                started_at=started,
                duration_seconds=0.0,
            )

        try:
            if plan.action is RemediationAction.RESTART_DEPLOYMENT:
                result = await self._restart(params)
            elif plan.action is RemediationAction.ROLLBACK_DEPLOYMENT:
                result = await self._rollback(params)
            elif plan.action is RemediationAction.SCALE_DEPLOYMENT:
                result = await self._scale(params)
            elif plan.action is RemediationAction.RESET_CHAOS_FAULT:
                result = await self._reset_chaos(params)
            else:
                # Unreachable: policy denies unknown actions. Kept so a new
                # enum member fails loudly rather than silently no-op'ing.
                raise RemediationRefused(
                    f"no executor for action {plan.action.value}"
                )
        except KubernetesUnavailable as exc:
            duration = time.time() - started
            observe_remediation(plan.action.value, "failure", duration)
            return RemediationResult(
                action=plan.action,
                params=params,
                succeeded=False,
                detail=f"Kubernetes API unavailable: {exc}",
                started_at=started,
                duration_seconds=duration,
            )
        except Exception as exc:  # noqa: BLE001 - k8s client raises ApiException & more
            duration = time.time() - started
            observe_remediation(plan.action.value, "failure", duration)
            logger.error(
                "remediation_failed",
                extra={
                    "action": plan.action.value,
                    "namespace": params.namespace,
                    "deployment": params.deployment,
                    "error_detail": str(exc)[:300],
                },
            )
            return RemediationResult(
                action=plan.action,
                params=params,
                succeeded=False,
                detail=f"{type(exc).__name__}: {str(exc)[:300]}",
                started_at=started,
                duration_seconds=duration,
            )

        result.started_at = started
        result.duration_seconds = time.time() - started
        observe_remediation(
            plan.action.value,
            "success" if result.succeeded else "failure",
            result.duration_seconds,
        )
        logger.info(
            "remediation_executed",
            extra={
                "action": plan.action.value,
                "namespace": params.namespace,
                "deployment": params.deployment,
                "succeeded": result.succeeded,
                "duration_seconds": result.duration_seconds,
            },
        )
        return result

    # ---- the four actions ------------------------------------------------
    async def _restart(self, params: ActionParams) -> RemediationResult:
        """Rollout restart via the restartedAt pod-template annotation."""
        before = await self.k8s.get_deployment(params.namespace, params.deployment)
        outcome = await self.k8s.restart_deployment(params.namespace, params.deployment)
        return RemediationResult(
            action=RemediationAction.RESTART_DEPLOYMENT,
            params=params,
            succeeded=True,
            detail=(
                "patched the pod template annotation "
                f"kubectl.kubernetes.io/restartedAt={outcome.get('restarted_at')}, "
                "which is exactly what `kubectl rollout restart` does. The "
                "Deployment controller now rolls the pods using the existing "
                "rolling-update strategy."
            ),
            before={"deployment": before},
        )

    async def _rollback(self, params: ActionParams) -> RemediationResult:
        """kubectl-rollout-undo equivalent: re-apply a historical template.

        We re-read the ReplicaSet list here rather than trusting the revision
        number carried in the plan. The plan was built during investigation,
        and a rollout could have completed in between — acting on a stale
        revision number is precisely the kind of race that turns a rollback
        into a roll-forward onto the broken version.
        """
        replicasets = await self.k8s.list_replicasets(
            params.namespace, params.deployment
        )
        target = find_previous_revision(replicasets, params.target_revision)
        if target is None:
            return RemediationResult(
                action=RemediationAction.ROLLBACK_DEPLOYMENT,
                params=params,
                succeeded=False,
                detail=(
                    "no ReplicaSet found for the rollback target "
                    f"(requested revision {params.target_revision}). The history "
                    "may have been pruned by revisionHistoryLimit since "
                    "investigation."
                ),
            )

        template = target.get("_template")
        if template is None:
            return RemediationResult(
                action=RemediationAction.ROLLBACK_DEPLOYMENT,
                params=params,
                succeeded=False,
                detail=(
                    f"ReplicaSet {target.get('name')} carried no pod template; "
                    "cannot reconstruct the rollback"
                ),
            )

        before = await self.k8s.get_deployment(params.namespace, params.deployment)
        outcome = await self.k8s.patch_deployment_template(
            params.namespace,
            params.deployment,
            template,
            int(target["revision"]),
        )
        return RemediationResult(
            action=RemediationAction.ROLLBACK_DEPLOYMENT,
            params=params,
            succeeded=True,
            detail=(
                f"patched the Deployment pod template back to revision "
                f"{target['revision']} (ReplicaSet {target.get('name')}, images "
                f"{target.get('images')}). This creates a NEW forward revision "
                "whose template matches the old one — Kubernetes has no concept "
                "of rewriting history, so the rollback is itself reversible."
            ),
            before={
                "deployment": before,
                "from_revision": (before or {}).get("current_revision"),
                "to_revision": target["revision"],
                "generation": outcome.get("generation"),
            },
        )

    async def _scale(self, params: ActionParams) -> RemediationResult:
        """Set spec.replicas, with the band re-asserted locally."""
        requested = params.replicas
        if requested is None:
            raise RemediationRefused("scale action reached the engine with no replica count")
        # Re-assert the band here as well as in policy. `max(min, ...)`
        # guarantees this can never write 0 even if every upstream check were
        # bypassed, because self.min_replicas is validated >= 1 at startup.
        clamped = max(self.min_replicas, min(self.max_replicas, int(requested)))
        if clamped < 1:
            raise RemediationRefused(
                f"refusing to scale {params.deployment} to {clamped}: scaling to "
                "zero is an outage, not a remediation"
            )

        before = await self.k8s.get_deployment(params.namespace, params.deployment)
        await self.k8s.scale_deployment(params.namespace, params.deployment, clamped)
        return RemediationResult(
            action=RemediationAction.SCALE_DEPLOYMENT,
            params=params,
            succeeded=True,
            detail=(
                f"set spec.replicas to {clamped} "
                f"(band [{self.min_replicas}, {self.max_replicas}]). On this "
                "single-node K3s cluster, replicas beyond what the node can "
                "schedule will sit Pending rather than adding capacity, which "
                "recovery validation will surface as pods not Ready."
            ),
            before={
                "deployment": before,
                "replicas": (before or {}).get("desired_replicas"),
            },
        )

    async def _reset_chaos(self, params: ActionParams) -> RemediationResult:
        """POST /api/chaos/reset on the target service.

        A plain reset clears the *entire* chaos state on that pod, which
        includes the `cpu_burn` and `memory_leak_mb` fields being added to
        POST /api/chaos/fault by another agent. That is exactly why this calls
        the blanket reset rather than posting a fault payload of zeroes — new
        fault fields need no change here.

        The `succeeded` flag reflects only whether the HTTP call was accepted.
        Whether the fault actually *cleared* is a question about the
        `chaos_*` gauges per `kubernetes_pod_name`, which the validation phase
        answers via chaos_client.verify_cleared(). Chaos state is per-pod
        in-memory, so a 200 through the Service VIP proves only that *some*
        replica cleared its state.
        """
        service = params.service or params.deployment
        base_url = self.base_url_resolver(service)
        if not base_url:
            return RemediationResult(
                action=RemediationAction.RESET_CHAOS_FAULT,
                params=params,
                succeeded=False,
                detail=(
                    f"no base URL is configured for service {service!r}, so there "
                    "is no chaos control plane to reset. Sentinel will not "
                    "construct an arbitrary URL for this."
                ),
            )

        outcome = await self.chaos.reset(base_url)
        return RemediationResult(
            action=RemediationAction.RESET_CHAOS_FAULT,
            params=params,
            succeeded=outcome.succeeded,
            detail=(
                outcome.detail
                + " Chaos state is per-pod in-memory; a 200 here means one replica "
                "cleared its state, so recovery validation re-checks the chaos_* "
                "gauges per kubernetes_pod_name before this is treated as fixed."
            ),
            before={"chaos_reset_response": outcome.to_dict()},
        )
