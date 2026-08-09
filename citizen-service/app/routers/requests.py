import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_citizen
from app.core.notifications import NotificationClient, get_notification_client
from app.models.citizen import Citizen
from app.models.request import ServiceRequest
from app.models.service import GovernmentService
from app.schemas.request import RequestCreate, RequestOut, RequestUpdate

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/requests", tags=["requests"])


@router.post("", response_model=RequestOut, status_code=status.HTTP_201_CREATED)
def create_request(
    payload: RequestCreate,
    background_tasks: BackgroundTasks,
    current_citizen: Citizen = Depends(get_current_citizen),
    db: Session = Depends(get_db),
    notifier: NotificationClient = Depends(get_notification_client),
):
    service = db.get(GovernmentService, payload.service_id)
    if service is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")

    req = ServiceRequest(citizen_id=current_citizen.id, service_id=service.id)
    db.add(req)
    db.commit()
    db.refresh(req)

    logger.info(
        "request_submitted",
        extra={"request_id": str(req.id), "citizen_id": str(current_citizen.id), "service_id": str(service.id)},
    )

    # Fire-and-forget, run after the response is sent so a slow/unavailable
    # Notification Service never adds latency to the citizen-facing call.
    background_tasks.add_task(
        notifier.send,
        citizen_id=req.citizen_id,
        request_id=req.id,
        event_type="request_submitted",
        recipient=current_citizen.email,
        message=f"Your request for '{service.name}' has been submitted and is pending review.",
    )

    return req


@router.get("", response_model=list[RequestOut])
def list_requests(
    current_citizen: Citizen = Depends(get_current_citizen),
    db: Session = Depends(get_db),
):
    return (
        db.query(ServiceRequest)
        .filter(ServiceRequest.citizen_id == current_citizen.id)
        .order_by(ServiceRequest.submission_date.desc())
        .all()
    )


@router.get("/{request_id}", response_model=RequestOut)
def get_request(
    request_id: uuid.UUID,
    current_citizen: Citizen = Depends(get_current_citizen),
    db: Session = Depends(get_db),
):
    req = db.get(ServiceRequest, request_id)
    if req is None or req.citizen_id != current_citizen.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    return req


@router.put("/{request_id}", response_model=RequestOut)
def update_request(
    request_id: uuid.UUID,
    payload: RequestUpdate,
    background_tasks: BackgroundTasks,
    current_citizen: Citizen = Depends(get_current_citizen),
    db: Session = Depends(get_db),
    notifier: NotificationClient = Depends(get_notification_client),
):
    req = db.get(ServiceRequest, request_id)
    if req is None or req.citizen_id != current_citizen.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")

    status_changed = payload.status is not None and payload.status != req.status
    if payload.status is not None:
        req.status = payload.status
    if payload.employee_note is not None:
        req.employee_note = payload.employee_note

    db.commit()
    db.refresh(req)

    logger.info("request_status_changed", extra={"request_id": str(req.id), "status": req.status.value})

    if status_changed:
        background_tasks.add_task(
            notifier.send,
            citizen_id=req.citizen_id,
            request_id=req.id,
            event_type="request_status_changed",
            recipient=current_citizen.email,
            message=f"Your request status changed to '{req.status.value}'.",
        )

    return req
