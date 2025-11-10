from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    DateTime,
    Boolean,
    ForeignKey,
    JSON,
)
from sqlalchemy.orm import relationship
from .base import BaseModel
from datetime import datetime


class Profile(BaseModel):
    __tablename__ = "profile"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("auth_user.id"), nullable=False)
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    email = Column(String(255), nullable=True)
    phone = Column(String(50), nullable=True)
    address = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    linkedin = Column(String, nullable=True)
    github = Column(String, nullable=True)
    links = Column(JSON, nullable=True, default={})
    # Relationships
    user = relationship("User", back_populates="profile")

    def to_dict(self):
        """Convert the model instance to a dictionary."""
        data = super().to_dict()
        # Add any additional serialization logic here if needed
        return data
