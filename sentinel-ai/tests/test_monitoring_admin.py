"""Tests for app/lifecycle/monitoring_admin.py."""
from __future__ import annotations

import pytest

from app.clients.loki import LokiClient
from app.clients.prometheus import PrometheusClient
from app.domain.environment import (
    ApplicationProfile,
    Environment,
    KubernetesConnectionConfig,
    LokiConnectionConfig,
    PrometheusConnectionConfig,
)
from app.lifecycle.monitoring_admin import (
    EDITABLE_FIELDS,
    apply_diffs,
    read_only_summary,
    snapshot,
    sync_environment_record,
    validate_changes,
)


class _FakeK8s:
    available = False
    init_error = "no in-cluster service account"


class _FakeStore:
    def __init__(self):
        self.saved = None

    def upsert_environment(self, record):
        self.saved = record


class _FakeCtx:
    """Minimal stand-in for SentinelContext — only the fields
    monitoring_admin touches."""

    def __init__(self):
        self.prom = PrometheusClient("http://prometheus:9090", timeout=10.0)
        self.loki = LokiClient("http://loki:3100", timeout=10.0)
        self.k8s = _FakeK8s()
        self.store = _FakeStore()
        self.environment = Environment(
            customer_id="cust-1",
            name="test-env",
            kubernetes=KubernetesConnectionConfig(mode="in_cluster"),
            prometheus=PrometheusConnectionConfig(url="http://prometheus:9090"),
            loki=LokiConnectionConfig(url="http://loki:3100"),
            application=ApplicationProfile(id="app-1", name="citizen-portal"),
        )


@pytest.fixture
def ctx():
    return _FakeCtx()


def test_editable_fields_never_include_credentials_or_kubernetes():
    assert set(EDITABLE_FIELDS) == {
        "prometheus_url",
        "prometheus_timeout_seconds",
        "loki_url",
        "loki_timeout_seconds",
    }
    for name in ("prometheus_bearer_token", "loki_bearer_token", "kubernetes_token", "kubeconfig_b64"):
        assert name not in EDITABLE_FIELDS


def test_snapshot_reflects_the_live_clients(ctx):
    assert snapshot(ctx) == {
        "prometheus_url": "http://prometheus:9090",
        "prometheus_timeout_seconds": 10.0,
        "loki_url": "http://loki:3100",
        "loki_timeout_seconds": 10.0,
    }


def test_read_only_summary_never_includes_a_bearer_token_value(ctx):
    ctx.environment.prometheus.bearer_token = "super-secret-token"
    summary = read_only_summary(ctx)
    assert "super-secret-token" not in str(summary)
    assert summary["prometheus_bearer_token_configured"] is True
    assert summary["loki_bearer_token_configured"] is False


def test_read_only_summary_never_includes_kubeconfig_or_k8s_token(ctx):
    ctx.environment.kubernetes = KubernetesConnectionConfig(
        mode="kubeconfig", kubeconfig_b64="c2VjcmV0LWtleQ=="
    )
    summary = read_only_summary(ctx)
    assert "c2VjcmV0LWtleQ==" not in str(summary)
    assert summary["kubernetes"]["kubeconfig_configured"] is True


def test_read_only_summary_surfaces_k8s_availability(ctx):
    summary = read_only_summary(ctx)
    assert summary["kubernetes"]["available"] is False
    assert summary["kubernetes"]["init_error"] == "no in-cluster service account"


def test_read_only_summary_health_checks_note_no_configurable_timeout(ctx):
    summary = read_only_summary(ctx)
    assert "No configurable timeout" in summary["health_checks"]["note"]


def test_attempting_to_set_a_bearer_token_is_rejected_with_a_clear_reason(ctx):
    errors, diffs = validate_changes(ctx, {"prometheus_bearer_token": "sk-whatever"})
    assert diffs == []
    assert "read-only" in errors[0]
    assert "POST /environments" in errors[0]


def test_attempting_to_set_kubernetes_fields_is_rejected(ctx):
    errors, diffs = validate_changes(ctx, {"kubernetes_namespace": "other-ns"})
    assert diffs == []
    assert "read-only" in errors[0]


def test_unknown_field_is_rejected(ctx):
    errors, diffs = validate_changes(ctx, {"grafana_url": "http://grafana:3000"})
    assert diffs == []
    assert "not a configurable monitoring value" in errors[0]


def test_empty_prometheus_url_is_rejected(ctx):
    errors, diffs = validate_changes(ctx, {"prometheus_url": ""})
    assert diffs == []
    assert "cannot be empty" in errors[0]


def test_url_without_scheme_is_rejected(ctx):
    errors, diffs = validate_changes(ctx, {"loki_url": "loki.internal:3100"})
    assert diffs == []
    assert "http://" in errors[0]


def test_valid_url_change_is_accepted_and_trailing_slash_is_stripped(ctx):
    errors, diffs = validate_changes(ctx, {"prometheus_url": "http://prometheus.new:9090/"})
    assert errors == []
    assert diffs[0].new_value == "http://prometheus.new:9090"


def test_timeout_within_bounds_is_accepted(ctx):
    errors, diffs = validate_changes(ctx, {"prometheus_timeout_seconds": 30})
    assert errors == []
    assert diffs[0].new_value == 30.0


def test_timeout_below_minimum_is_rejected(ctx):
    errors, diffs = validate_changes(ctx, {"loki_timeout_seconds": 0})
    assert diffs == []
    assert "between 1.0 and 60.0" in errors[0]


def test_timeout_above_maximum_is_rejected(ctx):
    errors, diffs = validate_changes(ctx, {"prometheus_timeout_seconds": 61})
    assert diffs == []
    assert "between 1.0 and 60.0" in errors[0]


def test_bool_timeout_is_rejected(ctx):
    errors, diffs = validate_changes(ctx, {"loki_timeout_seconds": True})
    assert diffs == []
    assert "must be a number" in errors[0]


def test_apply_diffs_mutates_the_live_clients_directly(ctx):
    _, diffs = validate_changes(ctx, {"prometheus_url": "http://prometheus.new:9090"})
    apply_diffs(ctx, diffs)
    assert ctx.prom.base_url == "http://prometheus.new:9090"
    # the OTHER client and the stored environment are untouched by this diff
    assert ctx.loki.base_url == "http://loki:3100"
    assert ctx.environment.prometheus.url == "http://prometheus:9090"


def test_sync_environment_record_updates_the_environment_and_persists_it(ctx):
    """The specific bug this category has that no prior category did:
    routers/environments.py reads straight from the STORED Environment
    record, not from ctx.prom/ctx.loki — so after_apply must update AND
    persist ctx.environment, or GET /environments/{id} and
    /test-connection would silently keep showing the old value forever."""
    _, diffs = validate_changes(
        ctx, {"prometheus_url": "http://prometheus.new:9090", "loki_timeout_seconds": 45}
    )
    apply_diffs(ctx, diffs)
    sync_environment_record(ctx, diffs)

    assert ctx.environment.prometheus.url == "http://prometheus.new:9090"
    assert ctx.environment.loki.timeout_seconds == 45.0
    assert ctx.store.saved is not None
    assert ctx.store.saved["prometheus"]["url"] == "http://prometheus.new:9090"
    assert ctx.store.saved["loki"]["timeout_seconds"] == 45.0


def test_sync_environment_record_is_a_no_op_for_no_diffs(ctx):
    sync_environment_record(ctx, [])
    assert ctx.store.saved is None


def test_sync_environment_record_never_persists_secrets_via_this_path(ctx):
    """Belt and suspenders: even though sync_environment_record only ever
    touches url/timeout fields, confirm the persisted record still carries
    the ORIGINAL bearer_token unchanged rather than, say, accidentally
    clobbering it to None."""
    ctx.environment.prometheus.bearer_token = "keep-me"
    _, diffs = validate_changes(ctx, {"prometheus_url": "http://prometheus.new:9090"})
    apply_diffs(ctx, diffs)
    sync_environment_record(ctx, diffs)
    assert ctx.store.saved["prometheus"]["bearer_token"] == "keep-me"
