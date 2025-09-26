from sqlalchemy import Column, Integer, String, Text, ForeignKey, Date
from sqlalchemy.orm import relationship
from .base import BaseModel


class ResumeExperience(BaseModel):
    __tablename__ = "resume_experience"
    id = Column(Integer, primary_key=True, autoincrement=True)
    resume_id = Column(Integer, ForeignKey("resume.id"), nullable=False)

    employer = Column(String, nullable=True)
    title = Column(String, nullable=True)
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    content = Column(Text, nullable=True)

    # Relationships
    resume = relationship("Resume", back_populates="experiences")
