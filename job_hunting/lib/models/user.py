# user model
# has username, email, salted password, id
from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.orm import relationship
from .base import BaseModel


class User(BaseModel):
    __tablename__ = "auth_user"
    __table_args__ = {"extend_existing": True}
    id = Column(Integer, primary_key=True, autoincrement=True)
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    email = Column(String, nullable=True)
    is_superuser = Column(Boolean, nullable=False, default=False)
    last_login = Column(DateTime)  # created_at is sufficient
    is_staff = Column(Boolean, nullable=False, default=False)
    # Relationships
    profile = relationship("Profile", back_populates="user", uselist=False)
    projects = relationship("Project", back_populates="user")
    resumes = relationship("Resume", back_populates="user")
    scores = relationship("Score")
    cover_letters = relationship("CoverLetter", back_populates="user")
    applications = relationship("Application", back_populates="user")
    summaries = relationship("Summary", back_populates="user")

    @property
    def name(self):
        """Return a display name for the user."""
        # Prefer "first_name last_name" (trimmed)
        if self.first_name or self.last_name:
            parts = []
            if self.first_name:
                parts.append(self.first_name.strip())
            if self.last_name:
                parts.append(self.last_name.strip())
            full_name = " ".join(parts).strip()
            if full_name:
                return full_name

        # Fallback to email
        if self.email:
            return self.email.strip()

        # Fallback to empty string
        return ""
