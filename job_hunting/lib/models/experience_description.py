#!/usr/bin/env python3

from sqlalchemy import Column, Integer, ForeignKey, UniqueConstraint
from .base import BaseModel


class ExperienceDescription(BaseModel):
    __tablename__ = "experience_description"

    id = Column(Integer, primary_key=True, autoincrement=True)
    experience_id = Column(Integer, ForeignKey("experience.id", ondelete="CASCADE"), nullable=False)
    description_id = Column(Integer, ForeignKey("description.id", ondelete="CASCADE"), nullable=False)
    order = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("experience_id", "description_id", name="uq_experience_description"),
    )
