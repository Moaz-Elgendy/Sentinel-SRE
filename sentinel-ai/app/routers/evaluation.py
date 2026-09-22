"""
Sentinel Agent Evaluation API.

  POST /api/evaluation/runs           — record a ground-truth expectation
                                         for a chaos scenario about to run
  GET  /api/evaluation/runs           — list runs, resolving any pending
                                         ones against the incident store
  GET  /api/evaluation/summary        — aggregate RCA-correctness /
                                         decision-accuracy metrics

This endpoint never triggers AWS SSM itself — that stays exactly where it
is (routers/chaos_scenarios.py). Recording a run only needs the scenario's
known expected outcome and a timestamp; resolving one only needs this same
incident store. See lifecycle/evaluation.py's module docstring for why that
separation exists and what "resolved" means.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core.deps import get_current_admin
from app.lifecycle import evaluation
from app.routers.chaos_scenarios import SCENARIOS

router = APIRouter(
    prefix="/api/evaluation",
    tags=["evaluation"],
    dependencies=[Depends(get_current_admin)],
)


class EvaluationRunRequest(BaseModel):
    scenario: str = Field(min_length=1)
    app: str | None = None


def _require_store(request: Request):
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="incident store is not ready",
        )
    return store


def _load_and_resolve_all(store: Any) -> list[evaluation.EvaluationRun]:
    """Every run, with any still-pending one given one more chance to link
    to an incident and, if that incident has since reached a terminal
    status, be scored — persisted once resolved so this is not recomputed
    on every subsequent read."""
    runs = [evaluation.EvaluationRun.from_dict(r) for r in store.list_evaluation_runs()]
    resolved: list[evaluation.EvaluationRun] = []
    for run in runs:
        updated = evaluation.resolve_run(run, store)
        if updated is not run:
            store.update_evaluation_run(updated.id, updated.to_dict())
        resolved.append(updated)
    return resolved


@router.post("/runs", status_code=status.HTTP_201_CREATED)
def create_evaluation_run(payload: EvaluationRunRequest, request: Request) -> dict[str, Any]:
    """Record that `scenario` is about to be (or was just) triggered, and
    what Sentinel is expected to conclude/do about it. Triggering the fault
    itself is a separate step — see routers/chaos_scenarios.py — so this
    call needs no AWS access and never fails because AWS is unreachable.
    """
    store = _require_store(request)
    meta = SCENARIOS.get(payload.scenario)
    if meta is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown scenario")
    if payload.scenario == "all":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'all' runs the whole suite, not one incident — record a run per scenario instead",
        )
    app = payload.app or meta.get("expected_app")
    if not app:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="this scenario has no default app — pass 'app' explicitly",
        )

    run = evaluation.create_run(
        run_id=f"eval-{uuid.uuid4().hex[:12]}",
        scenario=payload.scenario,
        app=app,
        expected_root_cause=meta.get("expected_root_cause"),
        expected_action=meta.get("expected_action"),
        triggered_at=time.time(),
    )
    store.create_evaluation_run(run.to_dict())
    return run.to_dict()


@router.get("/runs")
def list_evaluation_runs(request: Request) -> dict[str, Any]:
    store = _require_store(request)
    runs = _load_and_resolve_all(store)
    return {"count": len(runs), "runs": [r.to_dict() for r in runs]}


@router.get("/summary")
def evaluation_summary(request: Request) -> dict[str, Any]:
    store = _require_store(request)
    runs = _load_and_resolve_all(store)
    return evaluation.aggregate_evaluation_metrics(runs)
