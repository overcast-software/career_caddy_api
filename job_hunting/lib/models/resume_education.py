from sqlalchemy import Column, Integer, String, Text, ForeignKey, Date
from sqlalchemy.orm import relationship
from .base import BaseModel


class ResumeEducation(BaseModel):
    __tablename__ = "resume_education"
    id = Column(Integer, primary_key=True, autoincrement=True)
    resume_id = Column(Integer, ForeignKey("resume.id", ondelete="CASCADE"), nullable=False)
    education_id = Column(Integer, ForeignKey("education.id", ondelete="CASCADE"), nullable=False)
    institution = Column(String, nullable=True)
    degree = Column(String, nullable=True)
    issue_date = Column(Date, nullable=True)
    content = Column(Text, nullable=True)

    # Relationships
    resume = relationship("Resume", foreign_keys=[resume_id], viewonly=True)
    education = relationship("Education", foreign_keys=[education_id], viewonly=True)
