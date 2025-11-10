#!/usr/bin/env python3
from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from .base import BaseModel, Base
from datetime import datetime


class Project(BaseModel):
    __tablename__ = "project"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("auth_user.id"), nullable=False)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    start_date = Column(DateTime, nullable=True)
    end_date = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="projects")
    descriptions = relationship(
        "ProjectDescription",
        back_populates="project",
        order_by=lambda: Base.metadata.tables["project_description"].c.order,
        cascade="all, delete-orphan",
    )
