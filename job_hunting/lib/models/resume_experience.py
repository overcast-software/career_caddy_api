from sqlalchemy import Column, Integer, ForeignKey
from sqlalchemy.orm import relationship
from .base import BaseModel


class ResumeExperience(BaseModel):
    __tablename__ = "resume_experience"
    id = Column(Integer, primary_key=True, autoincrement=True)
    resume_id = Column(Integer, ForeignKey("resume.id"), nullable=False)
    experience_id = Column(Integer, ForeignKey("experience.id"), nullable=False)

    # Optional relationships (not required for the secondary mapping)
    resume = relationship("Resume")
    experience = relationship("Experience")
