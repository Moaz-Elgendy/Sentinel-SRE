import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Enum, String, Text
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class NotificationChannel(str, enum.Enum):
    email = "email"
    sms = "sms"


class NotificationStatus(str, enum.Enum):
    sent = "Sent"
    failed = "Failed"


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # citizen_id / request_id deliberately have no foreign key — they
    # reference rows owned by citizen-service's own database. Each
    # microservice owns its own data; cross-service references are IDs
    # only, never joins.
    citizen_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    request_id = Column(UUID(as_uuid=True), nullable=True, index=True)

    # e.g. "request_submitted", "request_status_changed". Free-form so
    # citizen-service (or future producers) can introduce new event types
    # without a migration here.
    event_type = Column(String(100), nullable=False)

    channel = Column(Enum(NotificationChannel), nullable=False, default=NotificationChannel.email)
    recipient = Column(String(255), nullable=False)
    message = Column(Text, nullable=False)

    status = Column(Enum(NotificationStatus), nullable=False, default=NotificationStatus.sent)
    error_detail = Column(Text, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)
