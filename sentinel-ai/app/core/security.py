"""
Sentinel SRE Control Center admin auth — password hashing and JWTs.

Deliberately the same shape as citizen-service's app/core/security.py (same
libraries, same pinned versions — see requirements.txt) but a SEPARATE
secret, a separate token audience, and a separate admin identity table
(`admins` in Sentinel's own SQLite store, not citizen-service's Postgres).
Mixing the two would mean a citizen JWT and an SRE admin JWT are
interchangeable, which they must never be — this is the boundary between
"can request a government service" and "can grant a temporary remediation
authorization".

This module knows nothing about HTTP or FastAPI — see app/core/deps.py for
the request-level dependency that uses `decode_access_token` to resolve
"who is calling this endpoint".
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(subject: str) -> str:
    """Mint a token whose `sub` is the admin's id (not username — usernames
    can change, ids don't)."""
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.sentinel_jwt_expire_minutes
    )
    payload = {"sub": subject, "exp": expire, "aud": "sentinel-gui"}
    return jwt.encode(
        payload, settings.sentinel_jwt_secret, algorithm=settings.sentinel_jwt_algorithm
    )


def decode_access_token(token: str) -> str | None:
    """Returns the admin id from a valid token, or None for anything else —
    expired, wrong secret, wrong audience, malformed. Never raises."""
    try:
        payload = jwt.decode(
            token,
            settings.sentinel_jwt_secret,
            algorithms=[settings.sentinel_jwt_algorithm],
            audience="sentinel-gui",
        )
        return payload.get("sub")
    except JWTError:
        return None
