import uuid

from sqlalchemy import Column, Integer, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.database import Base


class GovernmentService(Base):
    __tablename__ = "services"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(150), nullable=False, unique=True)
    description = Column(Text, nullable=False, default="")
    # JSON (not ARRAY) so the schema stays portable between Postgres (prod)
    # and SQLite (used for fast, dependency-free tests).
    required_documents = Column(JSON, nullable=False, default=list)
    estimated_processing_days = Column(Integer, nullable=False, default=7)

    requests = relationship("ServiceRequest", back_populates="service")
