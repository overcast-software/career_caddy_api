from sqlalchemy import Column, Integer, String, Text, Date, ForeignKey
from sqlalchemy.orm import relationship
from .base import BaseModel


class Education(BaseModel):
    __tablename__ = "education"
    id = Column(Integer, primary_key=True, autoincrement=True)

    degree = Column(String, nullable=True)
    issue_date = Column(Date, nullable=True)
    institution = Column(String, nullable=False)
    major = Column(String, nullable=False)
    minor = Column(String, nullable=False)
    resumes = relationship(
        "Resume",
        secondary="resume_education",
        back_populates="educations",
    )
