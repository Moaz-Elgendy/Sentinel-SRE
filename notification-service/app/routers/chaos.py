import secrets
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.chaos.state import ChaosState, controller
from app.core.config import settings

router = APIRouter(prefix="/api/chaos", tags=["chaos"])


class ChaosFaultRequest(BaseModel):
    latency_ms: int | None = Field(default=None, ge=0, le=30_000)
    error_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    db_failure: bool | None = None
    notification_failure_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    cpu_burn: bool | None = None
    # Upper bound is 2048 MiB, well above the pod's 256Mi limit on purpose:
    # leaking past the limit OOMKills the container, which is a legitimate
    # incident to demo rather than something to defend against. See the long
    # note on ChaosState.memory_leak_mb in app/chaos/state.py.
    memory_leak_mb: int | None = Field(default=None, ge=0, le=2048)


class ChaosStateOut(BaseModel):
    enabled: bool
    latency_ms: int
    error_rate: float
    db_failure: bool
    notification_failure_rate: float
    cpu_burn: bool
    memory_leak_mb: int


def _require_admin(x_chaos_token: Annotated[str | None, Header()] = None) -> None:
    configured = settings.chaos_admin_token
    if not settings.chaos_mode or not configured or not x_chaos_token or not secrets.compare_digest(
        x_chaos_token, configured
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _out(state: ChaosState) -> ChaosStateOut:
    return ChaosStateOut(
        enabled=settings.chaos_mode,
        latency_ms=state.latency_ms,
        error_rate=state.error_rate,
        db_failure=state.db_failure,
        notification_failure_rate=state.notification_failure_rate,
        cpu_burn=state.cpu_burn,
        memory_leak_mb=state.memory_leak_mb,
    )


@router.get("/status")
def chaos_status(x_chaos_token: Annotated[str | None, Header()] = None):
    _require_admin(x_chaos_token)
    return _out(controller.get())


@router.post("/fault", response_model=ChaosStateOut)
def set_chaos_fault(
    payload: ChaosFaultRequest,
    x_chaos_token: Annotated[str | None, Header()] = None,
):
    _require_admin(x_chaos_token)
    return _out(
        controller.update(
            latency_ms=payload.latency_ms,
            error_rate=payload.error_rate,
            db_failure=payload.db_failure,
            notification_failure_rate=payload.notification_failure_rate,
            cpu_burn=payload.cpu_burn,
            memory_leak_mb=payload.memory_leak_mb,
        )
    )


@router.post("/reset", response_model=ChaosStateOut)
def reset_chaos(x_chaos_token: Annotated[str | None, Header()] = None):
    _require_admin(x_chaos_token)
    return _out(controller.reset())
