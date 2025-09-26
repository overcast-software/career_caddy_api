from sqlalchemy import Column, Integer, ForeignKey
from sqlalchemy.orm import relationship
from .base import BaseModel


class ResumeSummary(BaseModel):
    __tablename__ = "resume_summary"
    id = Column(Integer, primary_key=True, autoincrement=True)
    resume_id = Column(Integer, ForeignKey("resume.id"), nullable=False)
    summary_id = Column(Integer, ForeignKey("summary.id"), nullable=False)

    # Relationships
    resume = relationship("Resume", back_populates="resume_summaries")
    summary = relationship("Summary", back_populates="resume_summaries")
