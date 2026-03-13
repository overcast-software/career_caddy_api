from sqlalchemy import Column, Integer, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime

from .base import BaseModel


class Answer(BaseModel):
    __tablename__ = "answer"

    id = Column(Integer, primary_key=True, autoincrement=True)
    question_id = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    favorite = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
