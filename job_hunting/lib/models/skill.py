from sqlalchemy import Column, Integer, Text
from sqlalchemy.orm import relationship
from .base import BaseModel

class Skill(BaseModel):
    __tablename__ = "skill"

    id = Column(Integer, primary_key=True, autoincrement=True)
    text = Column(Text, nullable=False)

    resumes = relationship(
        "Resume",
        secondary="resume_skill",
        back_populates="skills",
    )
    resume_skills = relationship(
        "ResumeSkill",
        back_populates="skill",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def to_export_value(self) -> str:
        """Return the skill text for export, or empty string if not meaningful."""
        text = getattr(self, "text", None)
        if text:
            return str(text)
        return str(self) if str(self) and str(self) != "None" else ""
