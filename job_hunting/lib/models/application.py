from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from .base import BaseModel


class Application(BaseModel):
    __tablename__ = "application"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("auth_user.id", ondelete="SET NULL"), nullable=True
    )
    job_post_id = Column(
        Integer, ForeignKey("job_post.id", ondelete="SET NULL"), nullable=True
    )
    company_id = Column(Integer, ForeignKey("company.id", ondelete="SET NULL"), nullable=True)
    resume_id = Column(Integer, ForeignKey("resume.id", ondelete="SET NULL"))
    cover_letter_id = Column(
        Integer, ForeignKey("cover_letter.id", ondelete="SET NULL")
    )
    applied_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String)  # e.g., submitted, interview, rejected, offer
    tracking_url = Column(String)  # link to ATS or application portal
    notes = Column(Text)
    company_id = Column(
        Integer, ForeignKey("company.id", ondelete="SET NULL"), nullable=True
    )

    # Relationships
    user = relationship("User", back_populates="applications")
    job_post = relationship("JobPost", back_populates="applications")
    company = relationship("Company", back_populates="applications")
    resume = relationship("Resume", back_populates="applications")
    cover_letter = relationship("CoverLetter", back_populates="application")
    company = relationship("Company", back_populates="applications")
    application_statuses = relationship(
        "JobApplicationStatus",
        back_populates="application",
        cascade="all, delete-orphan",
        order_by="JobApplicationStatus.created_at",
    )
    questions = relationship(
        "Question",
        back_populates="application",
        cascade="all, delete-orphan",
    )
