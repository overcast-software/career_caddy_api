from sqlalchemy import Column, Integer, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from .base import BaseModel


class JobApplicationStatus(BaseModel):
    __tablename__ = "job_application_status"
    id = Column(Integer, primary_key=True, autoincrement=True)
    application_id = Column(
        Integer, ForeignKey("application.id", ondelete="CASCADE"), nullable=False
    )
    status_id = Column(
        Integer, ForeignKey("status.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(DateTime, default=datetime.utcnow)
    note = Column(Text, nullable=True)

    # Relationships
    application = relationship("Application", back_populates="application_statuses")
    status = relationship("Status", back_populates="application_statuses")
