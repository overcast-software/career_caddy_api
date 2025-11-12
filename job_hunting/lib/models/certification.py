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
        overlaps="certification,resume",
    )

    def to_export_dict(self) -> dict:
        """Return a dict suitable for export templates."""
        cert_dict = {}
        cert_dict["title"] = getattr(self, "title", "") or ""
        cert_dict["issuer"] = getattr(self, "issuer", "") or ""
        cert_dict["content"] = getattr(self, "content", "") or ""
        
        issue_date = getattr(self, "issue_date", None)
        cert_dict["issue_date"] = str(issue_date) if issue_date else None
        
        return cert_dict
