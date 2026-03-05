from sqlalchemy import Column, Integer, ForeignKey
from sqlalchemy.orm import relationship
from .base import BaseModel


class ResumeProject(BaseModel):
    __tablename__ = "resume_project"
    id = Column(Integer, primary_key=True, autoincrement=True)
    resume_id = Column(Integer, ForeignKey("resume.id", ondelete="CASCADE"), nullable=False)
    project_id = Column(Integer, ForeignKey("project.id", ondelete="CASCADE"), nullable=False)
    order = Column(Integer, nullable=False, default=0)

    # Optional relationships (not required for the secondary mapping)
    resume = relationship("Resume")
    project = relationship("Project")
