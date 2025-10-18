from sqlalchemy import Column, Integer, ForeignKey, Boolean, UniqueConstraint
from sqlalchemy.orm import relationship
from .base import BaseModel

class ResumeSkill(BaseModel):
    __tablename__ = "resume_skill"

    id = Column(Integer, primary_key=True, autoincrement=True)
    resume_id = Column(Integer, ForeignKey("resume.id", ondelete="CASCADE"), index=True, nullable=False)
    skill_id = Column(Integer, ForeignKey("skill.id", ondelete="CASCADE"), index=True, nullable=False)
    active = Column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("resume_id", "skill_id", name="uq_resume_skill_resume_id_skill_id"),
    )

    resume = relationship("Resume", back_populates="resume_skills")
    skill = relationship("Skill", back_populates="resume_skills")
