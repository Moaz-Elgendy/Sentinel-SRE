from fastapi import APIRouter, Depends, Response, status

from app.chaos.state import controller
import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db

router = APIRouter(tags=["health"])


@router.get("/healthz")
def liveness():
    """Liveness probe: process is up. Must never touch the DB."""
    return {"status": "ok", "version": "0.1.0"}


@router.get("/readyz")
def readiness(response: Response, db: Session = Depends(get_db)):
    """Readiness probe: process AND its dependencies (DB) are ready."""
    checks = {}
    is_ready = True
    
    if controller.get().db_failure:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        checks["database"] = "down"
        return {"status": "not_ready", "checks": checks}

    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "up"
    except Exception:
        checks["database"] = "down"
        is_ready = False

    try:
        with httpx.Client(timeout=1.0) as client:
            resp = client.get(f"{settings.notification_service_url.rstrip('/')}/healthz")
            if resp.status_code == 200:
                checks["notification_service"] = "up"
            else:
                checks["notification_service"] = "down"
    except httpx.HTTPError:
        checks["notification_service"] = "down"

    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "checks": checks}
        
    status_str = "ready"
    if checks.get("notification_service") == "down":
        status_str = "degraded"
        checks["notification_service"] = "degraded"

    return {"status": status_str, "checks": checks}
