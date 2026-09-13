"""
Request-level auth dependency for the Sentinel SRE Control Center GUI API.

Every GUI-facing router (dashboard, actions, performance, meta, and the
existing incidents/environments routers once they are put behind this — see
main.py) depends on `get_current_admin`. It is intentionally the ONLY way
those routers learn who is calling: there is no alternate "trusted internal
caller" bypass, so a bug in one router cannot accidentally skip auth for
another.

This does not touch `alerts.py` (the public Alertmanager webhook) or
`chaos_scenarios.py` (its own pre-existing `X-Chaos-Token` gate) — both are
deliberately left as they are per the plan.
"""
from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.security import decode_access_token

bearer_scheme = HTTPBearer()


def _require_store(request: Request) -> Any:
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="incident store is not ready",
        )
    return store


def get_current_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict[str, Any]:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    admin_id = decode_access_token(credentials.credentials)
    if admin_id is None:
        raise unauthorized

    store = _require_store(request)
    admin = store.get_admin_by_id(admin_id)
    if admin is None:
        raise unauthorized

    # Never let a handler see the hash, even accidentally via **admin.
    admin.pop("password_hash", None)
    return admin
