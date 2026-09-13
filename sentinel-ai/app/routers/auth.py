"""
Sentinel SRE Control Center — admin auth.

  POST /api/auth/login  — username/password -> bearer JWT
  GET  /api/auth/me     — the calling admin's identity

This is the ONLY place a Sentinel GUI session is created. There is no
registration endpoint on purpose: admins are provisioned by whoever operates
Sentinel (the bootstrap admin created at startup — see main.py's lifespan —
or, later, an out-of-band admin-management step), not by anyone who can reach
this API. That mirrors the "SRE admin access only" requirement from the GUI
spec — a public self-registration flow would be the opposite of that.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.core.deps import get_current_admin
from app.core.security import create_access_token, verify_password

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class AdminOut(BaseModel):
    id: str
    username: str
    role: str


def _require_store(request: Request) -> Any:
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="incident store is not ready",
        )
    return store


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request) -> TokenResponse:
    store = _require_store(request)
    admin = store.get_admin_by_username(payload.username)

    if admin is None or not verify_password(payload.password, admin["password_hash"]):
        # Same message either way — do not reveal whether the username
        # exists.
        logger.info("sentinel_gui_login_failed", extra={"username": payload.username})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )

    store.touch_admin_login(admin["id"])
    token = create_access_token(subject=admin["id"])
    logger.info("sentinel_gui_login_succeeded", extra={"admin_id": admin["id"]})
    return TokenResponse(access_token=token)


@router.get("/me", response_model=AdminOut)
def me(current_admin: dict = Depends(get_current_admin)) -> dict:
    return current_admin


def bootstrap_admin_id() -> str:
    return f"admin-{uuid.uuid4().hex[:12]}"
