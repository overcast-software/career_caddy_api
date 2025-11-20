from sqlalchemy import Column, Integer, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from .base import BaseModel


class Question(BaseModel):
    __tablename__ = "question"
    id = Column(Integer, primary_key=True, autoincrement=True)
    application_id = Column(
        Integer, ForeignKey("application.id", ondelete="SET NULL"), nullable=True
    )
    company_id = Column(
        Integer, ForeignKey("company.id", ondelete="SET NULL"), nullable=True
    )
    created_by_id = Column(
        Integer, ForeignKey("auth_user.id", ondelete="SET NULL"), nullable=True
    )
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    application = relationship("Application", back_populates="questions")
    company = relationship("Company")
    user = relationship("User", foreign_keys=[created_by_id])
