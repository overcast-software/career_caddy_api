from sqlalchemy import Column, Integer, Text, ForeignKey, DateTime, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime
from .base import BaseModel


class CoverLetter(BaseModel):
    __tablename__ = "cover_letter"
    id = Column(Integer, primary_key=True, autoincrement=True)
    content = Column(Text, nullable=False)
    user_id = Column(Integer, ForeignKey("auth_user.id"))
    resume_id = Column(Integer, ForeignKey("resume.id"))
    job_post_id = Column(Integer)
    company_id = Column(Integer)
    favorite = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="cover_letters")
    resume = relationship("Resume", back_populates="cover_letters")
    application = relationship(
        "Application", back_populates="cover_letter", uselist=False
    )
