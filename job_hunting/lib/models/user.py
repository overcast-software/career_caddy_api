# user model
# has username, email, salted password, id
from sqlalchemy import Column, Integer, String, Boolean
from sqlalchemy.orm import relationship
from .base import BaseModel


class User(BaseModel):
    __tablename__ = "auth_user"
    id = Column(Integer, primary_key=True, autoincrement=True)
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    email = Column(String, unique=True, nullable=True)
    phone = Column(String, unique=True)
    is_admin = Column(Boolean, nullable=False, default=False)

    # Relationships
    resumes = relationship("Resume", back_populates="user")
    scores = relationship("Score")
    cover_letters = relationship("CoverLetter", back_populates="user")
    applications = relationship("Application", back_populates="user")
    summaries = relationship("Summary", back_populates="user")
