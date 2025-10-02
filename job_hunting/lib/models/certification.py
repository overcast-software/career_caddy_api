from sqlalchemy import Column, Integer, String, Text, Date
from sqlalchemy.orm import relationship
from .base import BaseModel


class Certification(BaseModel):
    __tablename__ = "certification"
    id = Column(Integer, primary_key=True, autoincrement=True)

    issuer = Column(String, nullable=True)
    title = Column(String, nullable=True)
    issue_date = Column(Date, nullable=True)
    content = Column(Text, nullable=True)

    # Many-to-many with Resume via resume_certification
    resumes = relationship(
        "Resume",
        secondary="resume_certification",
        back_populates="certifications",
    )
