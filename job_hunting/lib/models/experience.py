from sqlalchemy import Column, Integer, String, Text, Date, ForeignKey
from sqlalchemy.orm import relationship
from .base import BaseModel, Base


class Experience(BaseModel):
    __tablename__ = "experience"
    id = Column(Integer, primary_key=True, autoincrement=True)

    title = Column(String, nullable=True)
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    content = Column(Text, nullable=True)
    location = Column(String, nullable=True)
    summary = Column(String, nullable=True)
    company_id = Column(Integer, ForeignKey("company.id"), nullable=False)
    # Many-to-many with Resume via resume_experience
    resumes = relationship(
        "Resume",
        secondary="resume_experience",
        back_populates="experiences",
        overlaps="experience,resume",
    )
    company = relationship("Company")
    descriptions = relationship(
        "Description",
        secondary="experience_description",
        back_populates="experiences",
        order_by=lambda: Base.metadata.tables["experience_description"].c.order,
    )

    def to_export_dict(self) -> dict:
        """Return a dict suitable for export templates."""
        exp_dict = {}

        # Company name
        try:
            exp_dict["company"] = (
                getattr(getattr(self, "company", None), "name", "") or ""
            )
        except Exception:
            exp_dict["company"] = ""

        # Basic fields
        exp_dict["title"] = getattr(self, "title", "") or ""
        exp_dict["location"] = getattr(self, "location", "") or ""
        exp_dict["summary"] = self.summary

        # Dates
        start_date = getattr(self, "start_date", None)
        end_date = getattr(self, "end_date", None)
        exp_dict["start_date"] = str(start_date) if start_date else None
        exp_dict["end_date"] = str(end_date) if end_date else None

        # Date range
        if start_date and end_date:
            exp_dict["date_range"] = f"{start_date} – {end_date}"
        elif start_date:
            exp_dict["date_range"] = f"{start_date} – Present"
        else:
            exp_dict["date_range"] = ""

        # Descriptions
        descriptions = []
        try:
            for desc in getattr(self, "descriptions", []) or []:
                content = getattr(desc, "content", None)
                if content:
                    descriptions.append(str(content).strip())
        except Exception:
            pass
        exp_dict["descriptions"] = descriptions

        return exp_dict
