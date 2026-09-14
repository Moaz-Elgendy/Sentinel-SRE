"""
Sentinel Administration & Tuning Center — Policy configuration.

  GET  /api/config/policy                 current values + safety bounds
  POST /api/config/policy/preview         validate a proposed change, no side effects
  POST /api/config/policy/apply           validate + apply + audit
  GET  /api/config/history                append-only change history
  POST /api/config/history/{id}/restore   revert one past change (itself a new change)

Everything here is a thin I/O layer around app/lifecycle/policy_admin.py's
pure validate_changes/apply_diffs — this module's only jobs are: read the
request, call that module, write the audit trail, and never apply anything
`validate_changes` rejected. Re-reads `PolicyConfig.EDITABLE_FIELDS`'
bounds fresh on every request rather than trusting anything the client
sent, per the "frontend must never be trusted to enforce security"
requirement.

`preview` and `apply` both run the SAME validation — `apply` does not trust
a prior `preview` call (there is no session tying them together, and an
admin could call `apply` directly without ever calling `preview`). This is
the standard defense against a client showing one thing and sending another.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core.deps import get_current_admin
from app.lifecycle.policy_admin import (
    apply_diffs,
    bounds_metadata,
    snapshot,
    validate_changes,
)

router = APIRouter(prefix="/api/config", tags=["config"], dependencies=[Depends(get_current_admin)])

CATEGORY_POLICY = "policy"


class PolicyChangeRequest(BaseModel):
    changes: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None


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


@router.get("/policy")
def get_policy_config(request: Request) -> dict[str, Any]:
    ctx = _require_ctx(request)
    store = request.app.state.store
    return {
        "current": snapshot(ctx.policy.config),
        "bounds": bounds_metadata(),
        "protected": {
            # Shown for transparency ("what rules are controlling its
            # autonomy") but never accepted as input — see
            # policy_admin.py's module docstring.
            "denied_deployments": sorted(ctx.policy.config.denied_deployments),
            "denied_namespaces": sorted(ctx.policy.config.denied_namespaces),
        },
        **_latest_change_meta(store, CATEGORY_POLICY),
    }


@router.post("/policy/preview")
def preview_policy_change(payload: PolicyChangeRequest, request: Request) -> dict[str, Any]:
    ctx = _require_ctx(request)
    errors, diffs = validate_changes(
        ctx.policy.config,
        payload.changes,
        ctx.policy.config.denied_deployments,
        ctx.policy.config.denied_namespaces,
    )
    return {
        "valid": not errors,
        "errors": errors,
        "diff": [d.to_dict() for d in diffs],
    }


@router.post("/policy/apply")
def apply_policy_change(
    payload: PolicyChangeRequest,
    request: Request,
    current_admin: dict = Depends(get_current_admin),
) -> dict[str, Any]:
    ctx = _require_ctx(request)
    store = request.app.state.store

    if not payload.changes:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="no changes submitted")

    errors, diffs = validate_changes(
        ctx.policy.config,
        payload.changes,
        ctx.policy.config.denied_deployments,
        ctx.policy.config.denied_namespaces,
    )

    entry_id = f"cfg-{uuid.uuid4().hex[:12]}"
    now = time.time()

    if errors:
        store.create_config_history_entry(
            entry_id=entry_id,
            category=CATEGORY_POLICY,
            changes=[{"field": f, "old_value": None, "new_value": v} for f, v in payload.changes.items()],
            reason=payload.reason,
            admin_id=current_admin["id"],
            status="rejected",
            detail="; ".join(errors),
            created_at=now,
        )
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=errors)

    apply_diffs(ctx.policy.config, diffs)
    for diff in diffs:
        store.set_config_override(
            category=CATEGORY_POLICY,
            field_name=diff.field,
            value=diff.new_value,
            updated_at=now,
            updated_by=current_admin["id"],
        )
    store.create_config_history_entry(
        entry_id=entry_id,
        category=CATEGORY_POLICY,
        changes=[d.to_dict() for d in diffs],
        reason=payload.reason,
        admin_id=current_admin["id"],
        status="applied",
        detail=None,
        created_at=now,
    )

    return {
        "id": entry_id,
        "applied": [d.to_dict() for d in diffs],
        "current": snapshot(ctx.policy.config),
    }


@router.get("/history")
def list_config_history(
    request: Request, category: str | None = None, limit: int = 100, offset: int = 0
) -> dict[str, Any]:
    store = request.app.state.store
    entries = store.list_config_history(category=category, limit=limit, offset=offset)
    return {"history": entries}


@router.post("/history/{change_id}/restore")
def restore_config_change(
    change_id: str,
    request: Request,
    current_admin: dict = Depends(get_current_admin),
) -> dict[str, Any]:
    """Revert exactly one past change, by re-applying its `old_value`s as a
    brand-new change. This is itself a new, fully-validated,
    fully-audited apply — see this module's docstring — never a silent
    rewrite of history. If the bounds have changed since the original
    change (e.g. MAX_REPLICAS_CEILING was lowered in a later Sentinel
    version), the restore can legitimately fail validation just like any
    other apply would.
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

    revert_changes = {c["field"]: c["old_value"] for c in original["changes"]}
    errors, diffs = validate_changes(
        ctx.policy.config,
        revert_changes,
        ctx.policy.config.denied_deployments,
        ctx.policy.config.denied_namespaces,
    )

    entry_id = f"cfg-{uuid.uuid4().hex[:12]}"
    now = time.time()

    if errors:
        store.create_config_history_entry(
            entry_id=entry_id,
            category=original["category"],
            changes=[{"field": f, "old_value": None, "new_value": v} for f, v in revert_changes.items()],
            reason=f"restore of {change_id}",
            admin_id=current_admin["id"],
            status="rejected",
            detail="; ".join(errors),
            created_at=now,
            restores_change_id=change_id,
        )
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=errors)

    apply_diffs(ctx.policy.config, diffs)
    for diff in diffs:
        store.set_config_override(
            category=original["category"],
            field_name=diff.field,
            value=diff.new_value,
            updated_at=now,
            updated_by=current_admin["id"],
        )
    store.create_config_history_entry(
        entry_id=entry_id,
        category=original["category"],
        changes=[d.to_dict() for d in diffs],
        reason=f"restore of {change_id}",
        admin_id=current_admin["id"],
        status="applied",
        detail=None,
        created_at=now,
        restores_change_id=change_id,
    )

    return {
        "id": entry_id,
        "restores_change_id": change_id,
        "applied": [d.to_dict() for d in diffs],
        "current": snapshot(ctx.policy.config),
    }
