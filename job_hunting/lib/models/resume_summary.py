from sqlalchemy import Column, Integer, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from .base import BaseModel


class ResumeSummaries(BaseModel):
    __tablename__ = "resume_summaries"
    id = Column(Integer, primary_key=True, autoincrement=True)
    resume_id = Column(
        Integer, ForeignKey("resume.id", ondelete="CASCADE"), nullable=False
    )
    summary_id = Column(
        Integer, ForeignKey("summary.id", ondelete="CASCADE"), nullable=False
    )
    active = Column(Boolean)

    # Relationships
    resume = relationship("Resume", back_populates="resume_summaries")
    summary = relationship("Summary", back_populates="resume_summaries")
