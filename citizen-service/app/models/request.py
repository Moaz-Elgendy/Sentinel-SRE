import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Enum, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.database import Base


class RequestStatus(str, enum.Enum):
    pending = "Pending"
    under_review = "Under Review"
    approved = "Approved"
    rejected = "Rejected"
    completed = "Completed"


def _request_status_values(enum_cls: type[RequestStatus]) -> list[str]:
    return [status.value for status in enum_cls]


class ServiceRequest(Base):
    __tablename__ = "requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    citizen_id = Column(UUID(as_uuid=True), ForeignKey("citizens.id", ondelete="CASCADE"), nullable=False, index=True)
    service_id = Column(UUID(as_uuid=True), ForeignKey("services.id"), nullable=False, index=True)
    status = Column(
        Enum(RequestStatus, values_callable=_request_status_values),
        nullable=False,
        default=RequestStatus.pending,
    )
    submission_date = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    last_update = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)
    employee_note = Column(Text, nullable=True)

    citizen = relationship("Citizen", back_populates="requests")
    service = relationship("GovernmentService", back_populates="requests")
    documents = relationship("Document", back_populates="request", cascade="all, delete-orphan")

