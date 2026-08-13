import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.metrics import notification_deliveries_total
from app.core.sender import deliver
from app.models.notification import Notification
from app.schemas.notification import NotificationCreate, NotificationOut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.post("", response_model=NotificationOut, status_code=status.HTTP_201_CREATED)
def create_notification(payload: NotificationCreate, db: Session = Depends(get_db)):
    """Accept and "deliver" a notification.

    Always returns 201, even when simulated delivery fails — the failure
    is a fact about this notification's status, not about the request
    being invalid. Callers (e.g. citizen-service) that care about delivery
    outcome can inspect the returned `status` field; callers that don't
    can safely ignore it, matching the fire-and-forget way citizen-service
    calls this endpoint.
    """
    delivery_status, error_detail = deliver(
        channel=payload.channel.value, recipient=payload.recipient, message=payload.message
    )
    notification_deliveries_total.labels(channel=payload.channel.value, result=delivery_status.value).inc()

    notification = Notification(
        citizen_id=payload.citizen_id,
        request_id=payload.request_id,
        event_type=payload.event_type,
        channel=payload.channel,
        recipient=payload.recipient,
        message=payload.message,
        status=delivery_status,
        error_detail=error_detail,
    )
    db.add(notification)
    db.commit()
    db.refresh(notification)

    logger.info(
        "notification_processed",
        extra={
            "notification_id": str(notification.id),
            "event_type": notification.event_type,
            "status": notification.status.value,
        },
    )
    return notification


@router.get("", response_model=list[NotificationOut])
def list_notifications(
    citizen_id: uuid.UUID | None = Query(default=None),
    request_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    query = db.query(Notification)
    if citizen_id is not None:
        query = query.filter(Notification.citizen_id == citizen_id)
    if request_id is not None:
        query = query.filter(Notification.request_id == request_id)

    return query.order_by(Notification.created_at.desc()).limit(limit).all()


@router.get("/{notification_id}", response_model=NotificationOut)
def get_notification(notification_id: uuid.UUID, db: Session = Depends(get_db)):
    notification = db.get(Notification, notification_id)
    if notification is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
    return notification
