from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.orm import relationship
from datetime import datetime
from .base import BaseModel


class Status(BaseModel):
    __tablename__ = "status"
    id = Column(Integer, primary_key=True, autoincrement=True)
    status = Column(String, nullable=False)
    status_type = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    application_statuses = relationship(
        "JobApplicationStatus",
        back_populates="status",
        cascade="all, delete-orphan"
    )
