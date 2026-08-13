from fastapi import APIRouter, Depends, Response, status
import time
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_db

START_TIME = time.time()

router = APIRouter(tags=["health"])


@router.get("/healthz")
def liveness():
    """Liveness probe: process is up. Must never touch the DB."""
    return {"status": "ok", "version": "0.1.0"}


@router.get("/readyz")
def readiness(response: Response, db: Session = Depends(get_db)):
    """Readiness probe: process AND its dependencies (DB) are ready."""
    uptime_seconds = round(time.time() - START_TIME, 1)
    try:
        db.execute(text("SELECT 1"))
        return {
            "status": "ready",
            "checks": {"database": "up"},
            "version": "0.1.0",
            "uptime_seconds": uptime_seconds,
        }
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "not_ready",
            "checks": {"database": "down"},
            "version": "0.1.0",
            "uptime_seconds": uptime_seconds,
        }
