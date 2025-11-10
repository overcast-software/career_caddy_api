from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from .base import BaseModel
from datetime import datetime


class ProjectDescription(BaseModel):
    __tablename__ = "project_description"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("project.id"), nullable=False)
    description_id = Column(
        Integer, ForeignKey("description.id", ondelete="CASCADE"), nullable=False
    )
    order = Column(Integer)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    project = relationship("Project", back_populates="descriptions")
    description = relationship("Description", back_populates="project_descriptions")
