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

    def to_export_dict(self) -> dict:
        """Return a dict suitable for export templates."""
        edu_dict = {}
        edu_dict["institution"] = getattr(self, "institution", "") or ""
        edu_dict["degree"] = getattr(self, "degree", "") or ""
        edu_dict["major"] = getattr(self, "major", "") or ""
        edu_dict["minor"] = getattr(self, "minor", "") or ""
        
        issue_date = getattr(self, "issue_date", None)
        edu_dict["issue_date"] = str(issue_date) if issue_date else None
        
        return edu_dict
