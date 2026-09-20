"""
Sentinel Administration & Tuning Center — configuration.

  GET  /api/config/policy                 current policy values + safety bounds
  POST /api/config/policy/preview         validate a proposed policy change, no side effects
  POST /api/config/policy/apply           validate + apply + audit a policy change
  GET  /api/config/rca                    current RCA/validation thresholds + read-only detection info
  POST /api/config/rca/preview            validate a proposed RCA/validation change, no side effects
  POST /api/config/rca/apply              validate + apply + audit an RCA/validation change
  GET  /api/config/remediation            current remediation config (dry_run) + read-only action ladder
  POST /api/config/remediation/preview    validate a proposed remediation change, no side effects
  POST /api/config/remediation/apply      validate + apply + audit a remediation change
  GET  /api/config/ai                     current AI/reasoning config (provider/model/timeout/base_url)
  POST /api/config/ai/preview             validate a proposed AI/reasoning change, no side effects
  POST /api/config/ai/apply               validate + apply + audit an AI/reasoning change
  GET  /api/config/monitoring             current Prometheus/Loki config + read-only K8s/health info
  POST /api/config/monitoring/preview     validate a proposed monitoring change, no side effects
  POST /api/config/monitoring/apply       validate + apply + audit a monitoring change
  GET  /api/config/history                append-only change history, any category
  POST /api/config/history/{id}/restore   revert one past change (itself a new change)

Everything here is a thin I/O layer around the pure validate_changes/
apply_diffs pairs in app/lifecycle/policy_admin.py (category "policy"),
app/lifecycle/rca_admin.py (category "rca"), app/lifecycle/remediation_admin.py
(category "remediation"), app/lifecycle/ai_admin.py (category "ai"), and
app/lifecycle/monitoring_admin.py (category "monitoring") — this module's
only jobs are:
read the request, call the right pure module, write the audit trail, and
never apply anything `validate_changes` rejected. Every category is bounds-
checked fresh on every request rather than trusting anything the client
sent, per the "frontend must never be trusted to enforce security"
requirement.

`preview` and `apply` both run the SAME validation — `apply` does not trust
a prior `preview` call (there is no session tying them together, and an
admin could call `apply` directly without ever calling `preview`). This is
the standard defense against a client showing one thing and sending another.

Adding a new category (Remediation, AI/Reasoning, Monitoring — see the
approved Administration & Tuning Center plan's remaining phases) means:
write its own `<name>_admin.py` with the same `validate_changes(live_obj,
changes) -> (errors, diffs)` / `apply_diffs` / `reload_stored_overrides`
shape, register it in `_CATEGORIES` below, and add its GET/preview/apply
routes the same way `policy` and `rca` are wired here. `/history` and
`/history/{id}/restore` need no changes — they are already category-generic.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core.deps import get_current_admin
from app.lifecycle import ai_admin, monitoring_admin, policy_admin, rca_admin, remediation_admin

router = APIRouter(prefix="/api/config", tags=["config"], dependencies=[Depends(get_current_admin)])


class ChangeRequest(BaseModel):
    changes: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None


@dataclass
class CategoryHandler:
    """One entry per configuration category. `validate` and `apply_diffs`
    both take the LIVE config object for that category (e.g.
    `ctx.policy.config`, `ctx.validator.thresholds`) — never a copy — so an
    apply takes effect immediately, the same guarantee each `*_admin.py`
    module documents for itself."""

    get_live_object: Callable[[Any], Any]
    validate: Callable[[Any, dict[str, Any]], tuple[list[str], list[Any]]]
    apply_diffs: Callable[[Any, list[Any]], None]
    snapshot: Callable[[Any], dict[str, Any]]
    # Optional: called as after_apply(ctx, diffs) immediately after
    # apply_diffs, for a category whose fields are duplicated into OTHER
    # engines that must be kept in sync — see policy_admin.py's
    # sync_dependent_engines for why "policy" needs this and what it
    # deliberately does NOT do (collapse RemediationEngine's independent
    # re-check into a live read of PolicyConfig).
    after_apply: Callable[[Any, list[Any]], None] | None = None
    # Field names whose VALUE must never be persisted anywhere, including a
    # rejected-change audit entry — not just excluded from EDITABLE_FIELDS.
    # Only "ai" needs this today (see ai_admin.py: openai_api_key/
    # gemini_api_key are rejected as unknown fields like any other, but an
    # unknown-field rejection normally logs the submitted value verbatim
    # for audit purposes — see _apply_and_audit below — which would leak an
    # attempted key into GET /api/config/history in plaintext even though
    # it was never applied). Redacted, never merely omitted, so the history
    # entry still records that a value WAS submitted for that field.
    sensitive_fields: frozenset[str] = frozenset()


def _policy_validate(live_obj: Any, changes: dict[str, Any]) -> tuple[list[str], list[Any]]:
    return policy_admin.validate_changes(live_obj, changes, live_obj.denied_deployments, live_obj.denied_namespaces)


_CATEGORIES: dict[str, CategoryHandler] = {
    "policy": CategoryHandler(
        get_live_object=lambda ctx: ctx.policy.config,
        validate=_policy_validate,
        apply_diffs=policy_admin.apply_diffs,
        snapshot=policy_admin.snapshot,
        after_apply=policy_admin.sync_dependent_engines,
    ),
    "rca": CategoryHandler(
        get_live_object=lambda ctx: ctx.validator.thresholds,
        validate=rca_admin.validate_changes,
        apply_diffs=rca_admin.apply_diffs,
        snapshot=rca_admin.snapshot,
    ),
    "remediation": CategoryHandler(
        get_live_object=lambda ctx: ctx.remediation,
        validate=remediation_admin.validate_changes,
        apply_diffs=remediation_admin.apply_diffs,
        snapshot=remediation_admin.snapshot,
    ),
    "ai": CategoryHandler(
        get_live_object=lambda ctx: ctx.settings,
        validate=ai_admin.validate_changes,
        apply_diffs=ai_admin.apply_diffs,
        snapshot=ai_admin.snapshot,
        after_apply=ai_admin.rebuild_reasoner,
        sensitive_fields=frozenset({"openai_api_key", "gemini_api_key", "groq_api_key"}),
    ),
    "monitoring": CategoryHandler(
        get_live_object=lambda ctx: ctx,
        validate=monitoring_admin.validate_changes,
        apply_diffs=monitoring_admin.apply_diffs,
        snapshot=monitoring_admin.snapshot,
        after_apply=monitoring_admin.sync_environment_record,
        sensitive_fields=frozenset(
            {"prometheus_bearer_token", "loki_bearer_token", "kubernetes_token", "kubeconfig_b64"}
        ),
    ),
}


def _require_ctx(request: Request) -> Any:
    ctx = getattr(request.app.state, "context", None)
    if ctx is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Sentinel is not fully started yet"
        )
    return ctx


def _latest_change_meta(store: Any, category: str) -> dict[str, Any]:
    recent = store.list_config_history(category=category, limit=1)
    if not recent:
        return {"last_changed_at": None, "last_changed_by": None}
    return {"last_changed_at": recent[0]["created_at"], "last_changed_by": recent[0]["admin_id"]}


def _apply_and_audit(
    ctx: Any,
    category: str,
    handler: CategoryHandler,
    live_obj: Any,
    changes: dict[str, Any],
    reason: str | None,
    admin_id: str,
    store: Any,
    restores_change_id: str | None = None,
) -> dict[str, Any]:
    """Shared apply path for every category — validate, then either audit a
    rejection and raise, or apply + persist the override + audit a success.
    Used by both `POST /{category}/apply` and the restore endpoint."""
    errors, diffs = handler.validate(live_obj, changes)
    entry_id = f"cfg-{uuid.uuid4().hex[:12]}"
    now = time.time()

    if errors:
        store.create_config_history_entry(
            entry_id=entry_id,
            category=category,
            changes=[
                {
                    "field": f,
                    "old_value": None,
                    "new_value": "«redacted»" if f in handler.sensitive_fields else v,
                }
                for f, v in changes.items()
            ],
            reason=reason,
            admin_id=admin_id,
            status="rejected",
            detail="; ".join(errors),
            created_at=now,
            restores_change_id=restores_change_id,
        )
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=errors)

    handler.apply_diffs(live_obj, diffs)
    if handler.after_apply is not None:
        handler.after_apply(ctx, diffs)
    for diff in diffs:
        store.set_config_override(
            category=category,
            field_name=diff.field,
            value=diff.new_value,
            updated_at=now,
            updated_by=admin_id,
        )
    store.create_config_history_entry(
        entry_id=entry_id,
        category=category,
        changes=[d.to_dict() for d in diffs],
        reason=reason,
        admin_id=admin_id,
        status="applied",
        detail=None,
        created_at=now,
        restores_change_id=restores_change_id,
    )
    return {"id": entry_id, "applied": [d.to_dict() for d in diffs], "current": handler.snapshot(live_obj)}


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------
@router.get("/policy")
def get_policy_config(request: Request) -> dict[str, Any]:
    ctx = _require_ctx(request)
    store = request.app.state.store
    return {
        "current": policy_admin.snapshot(ctx.policy.config),
        "bounds": policy_admin.bounds_metadata(),
        "protected": {
            # Shown for transparency ("what rules are controlling its
            # autonomy") but never accepted as input — see
            # policy_admin.py's module docstring.
            "denied_deployments": sorted(ctx.policy.config.denied_deployments),
            "denied_namespaces": sorted(ctx.policy.config.denied_namespaces),
        },
        **_latest_change_meta(store, "policy"),
    }


@router.post("/policy/preview")
def preview_policy_change(payload: ChangeRequest, request: Request) -> dict[str, Any]:
    ctx = _require_ctx(request)
    errors, diffs = _policy_validate(ctx.policy.config, payload.changes)
    return {"valid": not errors, "errors": errors, "diff": [d.to_dict() for d in diffs]}


@router.post("/policy/apply")
def apply_policy_change(
    payload: ChangeRequest, request: Request, current_admin: dict = Depends(get_current_admin)
) -> dict[str, Any]:
    ctx = _require_ctx(request)
    if not payload.changes:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="no changes submitted")
    return _apply_and_audit(
        ctx,
        "policy",
        _CATEGORIES["policy"],
        ctx.policy.config,
        payload.changes,
        payload.reason,
        current_admin["id"],
        request.app.state.store,
    )


# ---------------------------------------------------------------------------
# RCA / Diagnosis
# ---------------------------------------------------------------------------
@router.get("/rca")
def get_rca_config(request: Request) -> dict[str, Any]:
    ctx = _require_ctx(request)
    store = request.app.state.store
    return {
        "current": rca_admin.snapshot(ctx.validator.thresholds),
        "bounds": rca_admin.bounds_metadata(),
        "read_only": rca_admin.read_only_summary(ctx.policy.config),
        **_latest_change_meta(store, "rca"),
    }


@router.post("/rca/preview")
def preview_rca_change(payload: ChangeRequest, request: Request) -> dict[str, Any]:
    ctx = _require_ctx(request)
    errors, diffs = rca_admin.validate_changes(ctx.validator.thresholds, payload.changes)
    return {"valid": not errors, "errors": errors, "diff": [d.to_dict() for d in diffs]}


@router.post("/rca/apply")
def apply_rca_change(
    payload: ChangeRequest, request: Request, current_admin: dict = Depends(get_current_admin)
) -> dict[str, Any]:
    ctx = _require_ctx(request)
    if not payload.changes:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="no changes submitted")
    return _apply_and_audit(
        ctx,
        "rca",
        _CATEGORIES["rca"],
        ctx.validator.thresholds,
        payload.changes,
        payload.reason,
        current_admin["id"],
        request.app.state.store,
    )


# ---------------------------------------------------------------------------
# Remediation
# ---------------------------------------------------------------------------
@router.get("/remediation")
def get_remediation_config(request: Request) -> dict[str, Any]:
    ctx = _require_ctx(request)
    store = request.app.state.store
    return {
        "current": remediation_admin.snapshot(ctx.remediation),
        "bounds": remediation_admin.bounds_metadata(),
        "read_only": remediation_admin.read_only_summary(),
        **_latest_change_meta(store, "remediation"),
    }


@router.post("/remediation/preview")
def preview_remediation_change(payload: ChangeRequest, request: Request) -> dict[str, Any]:
    ctx = _require_ctx(request)
    errors, diffs = remediation_admin.validate_changes(ctx.remediation, payload.changes)
    return {"valid": not errors, "errors": errors, "diff": [d.to_dict() for d in diffs]}


@router.post("/remediation/apply")
def apply_remediation_change(
    payload: ChangeRequest, request: Request, current_admin: dict = Depends(get_current_admin)
) -> dict[str, Any]:
    ctx = _require_ctx(request)
    if not payload.changes:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="no changes submitted")
    return _apply_and_audit(
        ctx,
        "remediation",
        _CATEGORIES["remediation"],
        ctx.remediation,
        payload.changes,
        payload.reason,
        current_admin["id"],
        request.app.state.store,
    )


# ---------------------------------------------------------------------------
# AI / Reasoning
# ---------------------------------------------------------------------------
@router.get("/ai")
def get_ai_config(request: Request) -> dict[str, Any]:
    ctx = _require_ctx(request)
    store = request.app.state.store
    return {
        "current": ai_admin.snapshot(ctx.settings),
        "bounds": ai_admin.bounds_metadata(),
        "read_only": ai_admin.read_only_summary(ctx.settings),
        **_latest_change_meta(store, "ai"),
    }


@router.post("/ai/preview")
def preview_ai_change(payload: ChangeRequest, request: Request) -> dict[str, Any]:
    ctx = _require_ctx(request)
    errors, diffs = ai_admin.validate_changes(ctx.settings, payload.changes)
    return {"valid": not errors, "errors": errors, "diff": [d.to_dict() for d in diffs]}


@router.post("/ai/apply")
def apply_ai_change(
    payload: ChangeRequest, request: Request, current_admin: dict = Depends(get_current_admin)
) -> dict[str, Any]:
    ctx = _require_ctx(request)
    if not payload.changes:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="no changes submitted")
    return _apply_and_audit(
        ctx,
        "ai",
        _CATEGORIES["ai"],
        ctx.settings,
        payload.changes,
        payload.reason,
        current_admin["id"],
        request.app.state.store,
    )


# ---------------------------------------------------------------------------
# Monitoring
# ---------------------------------------------------------------------------
@router.get("/monitoring")
def get_monitoring_config(request: Request) -> dict[str, Any]:
    ctx = _require_ctx(request)
    store = request.app.state.store
    return {
        "current": monitoring_admin.snapshot(ctx),
        "bounds": monitoring_admin.bounds_metadata(),
        "read_only": monitoring_admin.read_only_summary(ctx),
        **_latest_change_meta(store, "monitoring"),
    }


@router.post("/monitoring/preview")
def preview_monitoring_change(payload: ChangeRequest, request: Request) -> dict[str, Any]:
    ctx = _require_ctx(request)
    errors, diffs = monitoring_admin.validate_changes(ctx, payload.changes)
    return {"valid": not errors, "errors": errors, "diff": [d.to_dict() for d in diffs]}


@router.post("/monitoring/apply")
def apply_monitoring_change(
    payload: ChangeRequest, request: Request, current_admin: dict = Depends(get_current_admin)
) -> dict[str, Any]:
    ctx = _require_ctx(request)
    if not payload.changes:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="no changes submitted")
    return _apply_and_audit(
        ctx,
        "monitoring",
        _CATEGORIES["monitoring"],
        ctx,
        payload.changes,
        payload.reason,
        current_admin["id"],
        request.app.state.store,
    )


# ---------------------------------------------------------------------------
# History / restore — category-generic
# ---------------------------------------------------------------------------
@router.get("/history")
def list_config_history(
    request: Request, category: str | None = None, limit: int = 100, offset: int = 0
) -> dict[str, Any]:
    store = request.app.state.store
    entries = store.list_config_history(category=category, limit=limit, offset=offset)
    return {"history": entries}


@router.post("/history/{change_id}/restore")
def restore_config_change(
    change_id: str, request: Request, current_admin: dict = Depends(get_current_admin)
) -> dict[str, Any]:
    """Revert exactly one past change, by re-applying its `old_value`s as a
    brand-new change through the SAME `_apply_and_audit` path a live apply
    uses for that change's category — never a silent rewrite of history.
    If the bounds have changed since the original change (e.g. a later
    Sentinel version tightened one), the restore can legitimately fail
    validation just like any other apply would.
    """
    ctx = _require_ctx(request)
    store = request.app.state.store

    original = store.get_config_history_entry(change_id)
    if original is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="change not found")
    if original["status"] != "applied":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="only an applied change can be restored (this one was rejected and was never active)",
        )

    category = original["category"]
    handler = _CATEGORIES.get(category)
    if handler is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"restore is not available for category '{category}'",
        )

    revert_changes = {c["field"]: c["old_value"] for c in original["changes"]}
    live_obj = handler.get_live_object(ctx)
    result = _apply_and_audit(
        ctx,
        category,
        handler,
        live_obj,
        revert_changes,
        f"restore of {change_id}",
        current_admin["id"],
        store,
        restores_change_id=change_id,
    )
    result["restores_change_id"] = change_id
    return result
