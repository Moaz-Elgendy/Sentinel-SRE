"""
Sentinel Administration & Tuning Center — Monitoring configuration.

### What was actually inspected before writing this, and what it found

Per the spec's "Prometheus config, Loki config, Kubernetes connection
config, health-check config — with credentials never displayed," each of
the four was checked against the running code, not assumed:

* **Prometheus / Loki**: `PrometheusClient`/`LokiClient` (app/clients/) hold
  `self.base_url` and `self.timeout` as plain attributes, read fresh on
  every call inside `_get()` — NOT baked into a long-lived `httpx.Client`
  at construction. So, unlike the AI reasoner (Phase 5), no object needs to
  be torn down and rebuilt for a url/timeout change to take effect: setting
  the attribute directly on the live client IS the whole fix, live on the
  very next evidence-collection call. `bearer_token`, by contrast, IS baked
  into `self._headers` at construction — moot here anyway, since it is a
  credential and structurally excluded from `EDITABLE_FIELDS` regardless.
* Genuinely new finding: `timeout` had **no settings-backed or
  Environment-backed source at all** before this phase — `build_context()`
  simply never passed it, so it silently defaulted to the client's own
  hardcoded `10.0` with nothing to make an edit durable across a restart.
  `app/domain/environment.py`'s `PrometheusConnectionConfig`/
  `LokiConnectionConfig` gained a real `timeout_seconds: float = 10.0`
  field this phase (backward-compatible default for existing stored
  records) specifically to close that gap — see that module's comment.
* **The cross-object duplication bug this category actually has**: unlike
  every prior phase, Prometheus/Loki connection info is ALSO independently
  read straight from the stored `Environment` record — not from `ctx` — by
  `routers/environments.py`'s `GET /environments/{id}` and
  `POST /environments/{id}/test-connection`. Mutating only `ctx.prom`/
  `ctx.loki` would make THIS admin console correct while `test-connection`
  kept probing the OLD url forever. `sync_environment_record()` below (this
  category's `after_apply` hook) closes that by also updating
  `ctx.environment.prometheus`/`.loki` and persisting via
  `ctx.store.upsert_environment()` on every apply — which, as a side
  effect, ALSO makes the change durable across a restart through
  Environment's own existing reload path (`main.py` loads environments via
  `store.list_environments()` and calls `build_context()` FROM whichever
  one is active). That is why, unlike ai_admin.py/rca_admin.py/
  remediation_admin.py, this module has no `reload_stored_overrides` for
  main.py to call at startup: Environment already IS the durable store for
  these two fields, and adding a second, redundant persistence path
  (`config_overrides`) as the source of truth at boot would just be a new
  place for the two to disagree.
* **Kubernetes connection config: read-only here, on purpose.** The only
  genuinely non-secret fields are `mode`, `namespace`, `verify_ssl` — but
  changing any of them live means tearing down and re-establishing
  `KubernetesClient`'s underlying SDK connection (`initialise()`), against
  a cluster that may be entirely different from the one currently being
  watched, while incidents may be in flight. `POST /environments` already
  exists, deliberately, as the considered way to point Sentinel at a
  different cluster (see that router's module docstring on what "register
  a new environment" already means) — duplicating a thinner, riskier
  version of that here would not be a genuine capability, just a second
  door with less validation. `read_only_summary()` surfaces the same
  redacted view `Environment.to_public_dict()` already gives, so an admin
  can SEE the connection without this page pretending to let them touch it.
* **Health-check config: read-only, because there is nothing configurable
  to expose.** `routers/health.py`'s `/healthz`/`/readyz` are plain boolean
  checks (store reachable, Kubernetes available, dry_run vs autonomous,
  llm enabled vs rule-based) with no timeout or interval value anywhere in
  the code — any "timeout"/"periodSeconds" for these probes lives in the
  Kubernetes Deployment's own `livenessProbe`/`readinessProbe` spec
  (outside the application entirely), not in Sentinel's runtime config.
  Same "don't fake a knob that isn't real" discipline `rca_admin.py`
  applied to its hardcoded detection constants.

### Why `get_live_object` returns `ctx` itself, not a single sub-object

Every prior category modified one live object (`PolicyEngine`,
`RecoveryValidator.thresholds`, `RemediationEngine`, `Settings`). This one
modifies two (`ctx.prom` and `ctx.loki`) and needs a third (`ctx.environment`)
to keep in sync — so `validate_changes`/`apply_diffs`/`snapshot` below all
take `ctx` directly rather than a single field-holder, and `_CATEGORIES["monitoring"]`
registers `get_live_object=lambda ctx: ctx` accordingly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

EDITABLE_FIELDS: dict[str, str] = {
    "prometheus_url": "string",
    "prometheus_timeout_seconds": "float",
    "loki_url": "string",
    "loki_timeout_seconds": "float",
}

TIMEOUT_BOUNDS: dict[str, tuple[float, float]] = {
    "prometheus_timeout_seconds": (1.0, 60.0),
    "loki_timeout_seconds": (1.0, 60.0),
}

# Common typo/temptation targets — named explicitly so the rejection
# message points at the right place instead of a generic "not configurable."
_CREDENTIAL_OR_K8S_LOOKALIKES = (
    "prometheus_bearer_token",
    "loki_bearer_token",
    "kubernetes_mode",
    "kubernetes_namespace",
    "kubernetes_token",
    "kubeconfig_b64",
    "verify_ssl",
)


def bounds_metadata() -> dict[str, Any]:
    meta: dict[str, Any] = {}
    for field, kind in EDITABLE_FIELDS.items():
        if field in TIMEOUT_BOUNDS:
            lo, hi = TIMEOUT_BOUNDS[field]
            meta[field] = {"type": kind, "min": lo, "max": hi}
        else:
            meta[field] = {"type": kind}
    return meta


@dataclass
class FieldDiff:
    field: str
    old_value: Any
    new_value: Any
    warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "warning": self.warning,
        }


def snapshot(ctx: Any) -> dict[str, Any]:
    return {
        "prometheus_url": ctx.prom.base_url,
        "prometheus_timeout_seconds": ctx.prom.timeout,
        "loki_url": ctx.loki.base_url,
        "loki_timeout_seconds": ctx.loki.timeout,
    }


def read_only_summary(ctx: Any) -> dict[str, Any]:
    public = ctx.environment.to_public_dict()
    return {
        "description": (
            "URL and timeout for Prometheus and Loki are editable below. Kubernetes "
            "connection details and both providers' bearer tokens are read-only here — "
            "credentials are never readable or writable through this API. To point "
            "Sentinel at a different cluster or rotate a token, use POST /environments."
        ),
        "kubernetes": {
            **public["kubernetes"],
            "available": ctx.k8s.available,
            "init_error": None if ctx.k8s.available else ctx.k8s.init_error,
        },
        "prometheus_bearer_token_configured": public["prometheus"]["bearer_token_configured"],
        "loki_bearer_token_configured": public["loki"]["bearer_token_configured"],
        "health_checks": {
            "description": (
                "/healthz and /readyz (app/routers/health.py) are plain boolean checks — "
                "store reachable, Kubernetes available, dry_run vs autonomous, llm enabled "
                "vs rule_based_only."
            ),
            "checked": ["store", "kubernetes", "mode", "llm"],
            "note": (
                "No configurable timeout or interval value exists for these probes in "
                "Sentinel's own runtime config — periodSeconds/timeoutSeconds for "
                "/healthz and /readyz are set in the Kubernetes Deployment's probe spec, "
                "outside this console's scope."
            ),
        },
    }


def _validate_url(field: str, new_value: Any) -> tuple[str | None, FieldDiff | None]:
    if not isinstance(new_value, str) or not new_value.strip():
        return f"'{field}' cannot be empty.", None
    if not (new_value.startswith("http://") or new_value.startswith("https://")):
        return f"'{field}' must start with http:// or https://.", None
    return None, None


def validate_changes(ctx: Any, changes: dict[str, Any]) -> tuple[list[str], list[FieldDiff]]:
    errors: list[str] = []
    diffs: list[FieldDiff] = []

    unknown = set(changes) - set(EDITABLE_FIELDS)
    for field in sorted(unknown):
        if field in _CREDENTIAL_OR_K8S_LOOKALIKES:
            errors.append(
                f"'{field}' is not editable here — Kubernetes connection details and "
                "provider bearer tokens are read-only in this console. Use "
                "POST /environments to change them."
            )
        else:
            errors.append(f"'{field}' is not a configurable monitoring value.")

    if "prometheus_url" in changes:
        new_value = changes["prometheus_url"]
        error, _ = _validate_url("prometheus_url", new_value)
        if error:
            errors.append(error)
        else:
            diffs.append(FieldDiff("prometheus_url", ctx.prom.base_url, new_value.rstrip("/")))

    if "loki_url" in changes:
        new_value = changes["loki_url"]
        error, _ = _validate_url("loki_url", new_value)
        if error:
            errors.append(error)
        else:
            diffs.append(FieldDiff("loki_url", ctx.loki.base_url, new_value.rstrip("/")))

    field_to_client = {"prometheus_timeout_seconds": ctx.prom, "loki_timeout_seconds": ctx.loki}
    for field, client in field_to_client.items():
        if field in changes:
            new_value = changes[field]
            lo, hi = TIMEOUT_BOUNDS[field]
            if isinstance(new_value, bool) or not isinstance(new_value, (int, float)):
                errors.append(f"'{field}' must be a number.")
            elif not (lo <= float(new_value) <= hi):
                errors.append(f"'{field}' must be between {lo} and {hi} seconds.")
            else:
                diffs.append(FieldDiff(field, client.timeout, float(new_value)))

    return errors, diffs


def apply_diffs(ctx: Any, diffs: list[FieldDiff]) -> None:
    for diff in diffs:
        if diff.field == "prometheus_url":
            ctx.prom.base_url = diff.new_value
        elif diff.field == "prometheus_timeout_seconds":
            ctx.prom.timeout = diff.new_value
        elif diff.field == "loki_url":
            ctx.loki.base_url = diff.new_value
        elif diff.field == "loki_timeout_seconds":
            ctx.loki.timeout = diff.new_value


def sync_environment_record(ctx: Any, diffs: list[FieldDiff]) -> None:
    """`after_apply` hook — see this module's docstring for why the stored
    `Environment` record must be kept in sync with the live `ctx.prom`/
    `ctx.loki` clients, and why that also means restart-durability comes
    from Environment's own persistence rather than a separate
    `reload_stored_overrides` step."""
    if not diffs:
        return
    for diff in diffs:
        if diff.field == "prometheus_url":
            ctx.environment.prometheus.url = diff.new_value
        elif diff.field == "prometheus_timeout_seconds":
            ctx.environment.prometheus.timeout_seconds = diff.new_value
        elif diff.field == "loki_url":
            ctx.environment.loki.url = diff.new_value
        elif diff.field == "loki_timeout_seconds":
            ctx.environment.loki.timeout_seconds = diff.new_value
    ctx.store.upsert_environment(ctx.environment.to_dict())
