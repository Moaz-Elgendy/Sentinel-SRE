from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_db

router = APIRouter(tags=["health"])


@router.get("/healthz")
def liveness():
    """Liveness probe: process is up. Must never touch the DB."""
    return {"status": "ok"}


@router.get("/readyz")
def readiness(response: Response, db: Session = Depends(get_db)):
    """Readiness probe: process AND its dependencies (DB) are ready."""
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ready", "database": "up"}
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "database": "down"}
