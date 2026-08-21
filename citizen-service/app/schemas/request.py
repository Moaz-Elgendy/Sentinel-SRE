import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.request import RequestStatus


class RequestCreate(BaseModel):
    service_id: uuid.UUID


class RequestUpdate(BaseModel):
    status: RequestStatus | None = None
    employee_note: str | None = None


class RequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    citizen_id: uuid.UUID
    service_id: uuid.UUID
    status: RequestStatus
    submission_date: datetime
    last_update: datetime
    employee_note: str | None
