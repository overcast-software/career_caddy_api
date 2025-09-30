from sqlalchemy import Column, Integer, String, Text, Date
from sqlalchemy.orm import relationship
from .base import BaseModel


class Experience(BaseModel):
    __tablename__ = "experience"
    id = Column(Integer, primary_key=True, autoincrement=True)

    title = Column(String, nullable=True)
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    summary = Column(Text, nullable=True)
    content = Column(Text, nullable=True)

    # Many-to-many with Resume via resume_experience
    resumes = relationship(
        "Resume",
        secondary="resume_experience",
        back_populates="experiences",
    )
