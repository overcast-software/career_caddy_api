from sqlalchemy import Column, Integer, String, Text, ForeignKey, Date
from sqlalchemy.orm import relationship
from .base import BaseModel


class ResumeCertification(BaseModel):
    __tablename__ = "resume_certification"
    id = Column(Integer, primary_key=True, autoincrement=True)
    resume_id = Column(Integer, ForeignKey("resume.id"), nullable=False)
    certification_id = Column(Integer, ForeignKey("certification.id"), nullable=False)

    issuer = Column(String, nullable=True)
    title = Column(String, nullable=True)
    issue_date = Column(Date, nullable=True)
    content = Column(Text, nullable=True)

    # Relationships (view-only to avoid conflicting with the many-to-many)
    resume = relationship("Resume", foreign_keys=[resume_id], viewonly=True)
    certification = relationship("Certification", foreign_keys=[certification_id], viewonly=True)
