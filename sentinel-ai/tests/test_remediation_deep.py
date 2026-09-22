"""
RemediationEngine.execute_deep() tests: the typed-execution boundary for
Deep Investigation proposals. Mirrors test_remediation_allowlist.py's style
for the four known actions.
"""
from __future__ import annotations

import pytest

from app.lifecycle.remediation import RemediationEngine, RemediationRefused
from app.models.incident import DeepActionTarget, DeepPolicyVerdict, NovelActionType


def _engine(fake_k8s, fake_chaos, **overrides):
    defaults = dict(
        k8s=fake_k8s,
        chaos=fake_chaos,
        allowed_namespaces=frozenset({"citizen-portal"}),
        allowed_deployments=frozenset({"citizen-service"}),
        denied_deployments=frozenset({"citizen-postgres"}),
        denied_namespaces=frozenset({"kube-system"}),
        min_replicas=1,
        max_replicas=3,
    )
    defaults.update(overrides)
    return RemediationEngine(**defaults)


def _target(**overrides):
    defaults = dict(
        namespace="citizen-portal", deployment="citizen-service",
        container="citizen-service", key="DATABASE_HOST", value="good-host",
        previous_value="bad-host", previous_value_existed=True,
    )
    defaults.update(overrides)
    return DeepActionTarget(**defaults)


@pytest.mark.asyncio
async def test_set_env_var_executes_the_typed_write(fake_k8s, fake_chaos):
    engine = _engine(fake_k8s, fake_chaos)
    target = _target()
    verdict = DeepPolicyVerdict(allowed=True, action_type=NovelActionType.SET_ENV_VAR)
    result = await engine.execute_deep(target, verdict)
    assert result.succeeded is True
    assert fake_k8s.writes == [
        ("set_env_var", {"namespace": "citizen-portal", "name": "citizen-service",
                          "container": "citizen-service", "key": "DATABASE_HOST", "value": "good-host"})
    ]


@pytest.mark.asyncio
async def test_unset_env_var_executes_the_typed_removal(fake_k8s, fake_chaos):
    engine = _engine(fake_k8s, fake_chaos)
    target = _target(value=None)
    verdict = DeepPolicyVerdict(allowed=True, action_type=NovelActionType.UNSET_ENV_VAR)
    result = await engine.execute_deep(target, verdict)
    assert result.succeeded is True
    assert fake_k8s.writes == [
        ("unset_env_var", {"namespace": "citizen-portal", "name": "citizen-service",
                            "container": "citizen-service", "key": "DATABASE_HOST"})
    ]


@pytest.mark.asyncio
async def test_execute_deep_refuses_an_unauthorised_verdict(fake_k8s, fake_chaos):
    engine = _engine(fake_k8s, fake_chaos)
    target = _target()
    verdict = DeepPolicyVerdict(allowed=False, action_type=NovelActionType.SET_ENV_VAR)
    with pytest.raises(RemediationRefused):
        await engine.execute_deep(target, verdict)
    assert fake_k8s.writes == []


@pytest.mark.asyncio
async def test_execute_deep_re_checks_the_allowlist_independently_of_policy(fake_k8s, fake_chaos):
    """Even an `allowed=True` verdict for a target outside this engine's own
    frozen allow-list must be refused — the same independent-gate guarantee
    `execute()` already has for the four known actions."""
    engine = _engine(fake_k8s, fake_chaos)
    target = _target(deployment="some-other-deployment")
    verdict = DeepPolicyVerdict(allowed=True, action_type=NovelActionType.SET_ENV_VAR)
    with pytest.raises(RemediationRefused):
        await engine.execute_deep(target, verdict)
    assert fake_k8s.writes == []


@pytest.mark.asyncio
async def test_execute_deep_re_checks_the_denylist_independently_of_policy(fake_k8s, fake_chaos):
    engine = _engine(fake_k8s, fake_chaos)
    target = _target(namespace="kube-system")
    verdict = DeepPolicyVerdict(allowed=True, action_type=NovelActionType.SET_ENV_VAR)
    with pytest.raises(RemediationRefused):
        await engine.execute_deep(target, verdict)
    assert fake_k8s.writes == []


@pytest.mark.asyncio
async def test_execute_deep_re_checks_the_sensitive_env_key_rule_independently_of_policy(fake_k8s, fake_chaos):
    """Independent re-check of the same rule deep_investigation.
    apply_llm_response and PolicyEngine.evaluate_deep_proposal already
    apply — a target naming a credential-looking key should never actually
    reach this method, but if one does, execution must still refuse it as
    the final gate before the cluster write."""
    engine = _engine(fake_k8s, fake_chaos)
    target = _target(key="API_TOKEN", value="new-token")
    verdict = DeepPolicyVerdict(allowed=True, action_type=NovelActionType.SET_ENV_VAR)
    with pytest.raises(RemediationRefused):
        await engine.execute_deep(target, verdict)
    assert fake_k8s.writes == []


@pytest.mark.asyncio
async def test_dry_run_never_touches_the_cluster(fake_k8s, fake_chaos):
    engine = _engine(fake_k8s, fake_chaos, dry_run=True)
    target = _target()
    verdict = DeepPolicyVerdict(allowed=True, action_type=NovelActionType.SET_ENV_VAR)
    result = await engine.execute_deep(target, verdict)
    assert result.succeeded is True
    assert result.dry_run is True
    assert fake_k8s.writes == []
