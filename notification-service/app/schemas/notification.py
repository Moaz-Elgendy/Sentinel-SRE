import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.notification import NotificationChannel, NotificationStatus


class NotificationCreate(BaseModel):
    citizen_id: uuid.UUID
    request_id: uuid.UUID | None = None
    event_type: str = Field(min_length=1, max_length=100)
    channel: NotificationChannel = NotificationChannel.email
    recipient: str = Field(min_length=1, max_length=255)
    message: str = Field(min_length=1, max_length=2000)


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    citizen_id: uuid.UUID
    request_id: uuid.UUID | None
    event_type: str
    channel: NotificationChannel
    recipient: str
    message: str
    status: NotificationStatus
    error_detail: str | None
    created_at: datetime
