from sqlalchemy import Column, Integer, Text, String
from sqlalchemy.orm import relationship
from .base import BaseModel


class Skill(BaseModel):
    __tablename__ = "skill"

    id = Column(Integer, primary_key=True, autoincrement=True)
    text = Column(Text, nullable=False)
    skill_type = Column(String, nullable=True)

    resumes = relationship(
        "Resume",
        secondary="resume_skill",
        back_populates="skills",
        overlaps="resume_skills,resume",
    )
    resume_skills = relationship(
        "ResumeSkill",
        back_populates="skill",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def to_export_value(self) -> dict:
        """Return the skill text and type for export as a dictionary."""
        text = getattr(self, "text", None)
        skill_type = getattr(self, "skill_type", None)
        
        # Handle text - return empty string if missing
        if text:
            text_value = str(text)
        else:
            fallback = str(self) if str(self) and str(self) != "None" else ""
            text_value = fallback
        
        # Handle skill_type - return None if missing or empty
        if skill_type and str(skill_type).strip():
            type_value = str(skill_type)
        else:
            type_value = None
            
        return {"text": text_value, "skill_type": type_value}
