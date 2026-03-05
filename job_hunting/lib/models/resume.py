from typing import Optional, TYPE_CHECKING
from sqlalchemy import Column, Integer, Text, ForeignKey, String, Boolean
from sqlalchemy.orm import relationship
from .base import BaseModel, Base

if TYPE_CHECKING:
    from .summary import Summary


class Resume(BaseModel):
    __tablename__ = "resume"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("auth_user.id"))
    file_path = Column(String)
    title = Column(String)
    name = Column(String)  # internal name
    notes = Column(Text)  # notes about this flavor resume
    favorite = Column(Boolean, default=False, nullable=False)
    # Relationships
    user = relationship("User", back_populates="resumes")
    scores = relationship("Score", back_populates="resume")
    cover_letters = relationship("CoverLetter", back_populates="resume")
    applications = relationship("Application", back_populates="resume")
    summaries = relationship(
        "Summary",
        secondary="resume_summaries",
        back_populates="resumes",
        overlaps="resume_summaries,summary",
        passive_deletes=True,
    )

    resume_summaries = relationship(
        "ResumeSummaries",
        back_populates="resume",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    experiences = relationship(
        "Experience",
        secondary="resume_experience",
        back_populates="resumes",
        overlaps="experience,resume",
        order_by=lambda: Base.metadata.tables["resume_experience"].c.order,
    )

    projects = relationship(
        "Project",
        secondary="resume_project",
        back_populates="resumes",
        overlaps="project,resume",
        order_by=lambda: Base.metadata.tables["resume_project"].c.order,
    )

    certifications = relationship(
        "Certification",
        secondary="resume_certification",
        back_populates="resumes",
    )

    educations = relationship(
        "Education",
        secondary="resume_education",
        back_populates="resumes",
    )

    skills = relationship(
        "Skill",
        secondary="resume_skill",
        back_populates="resumes",
        overlaps="resume_skills,skill",
    )

    resume_skills = relationship(
        "ResumeSkill",
        back_populates="resume",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @classmethod
    def from_path_and_user_id(cls, path, user_id):
        with open(path) as file:
            body = file.read()
            resume, _ = cls.first_or_create(file_path=path, user_id=user_id)
        return resume

    def language_skills(self):
        self.skills_by_type("Language")

    def database_skills(self):
        self.skills_by_type("Database")

    def framework_skills(self):
        self.skills_by_type("Framework")

    def tool_skills(self):
        self.skills_by_type("Tools/Platform")

    def security_skills(self):
        self.skills_by_type("Security")

    def skills_by_type(self, skill_type):
        return [skill for skill in self.skills if skill.skill_type == skill_type]

    def active_summary(self) -> Optional["Summary"]:
        """Return the linked Summary where ResumeSummaries.active == True if present;
        otherwise the first linked summary; otherwise None."""
        try:
            # Find active summary
            active_link = next(
                (
                    l
                    for l in (getattr(self, "resume_summaries", []) or [])
                    if getattr(l, "active", False)
                ),
                None,
            )
            if active_link and getattr(active_link, "summary", None):
                return active_link.summary

            # Fallback to first linked summary if no active one
            if getattr(self, "resume_summaries", None):
                first_link = next(iter(self.resume_summaries), None)
                if first_link and getattr(first_link, "summary", None):
                    return first_link.summary
        except Exception:
            pass
        return None

    def active_summary_content(self) -> str:
        """Return content from active_summary() or empty string."""
        summary = self.active_summary()
        if summary:
            return getattr(summary, "content", "") or ""
        return ""

    def to_export_context(self) -> dict:
        """Build and return the template context dict for export."""
        context = {}

        # Header
        header = {}
        if self.user:
            header["name"] = self.user.name
            header["phone"] = getattr(self.user, "phone", "")
        else:
            header["name"] = ""
            header["phone"] = ""
        header["title"] = getattr(self, "title", "") or ""
        context["header"] = header
        context["HEADER"] = header

        # Summary
        context["summary"] = self.active_summary_content().strip()

        # Experiences
        experiences = []
        try:
            for exp in getattr(self, "experiences", []) or []:
                experiences.append(exp.to_export_dict())
        except Exception:
            pass
        context["experiences"] = experiences

        # Educations
        educations = []
        try:
            for edu in getattr(self, "educations", []) or []:
                educations.append(edu.to_export_dict())
        except Exception:
            pass
        context["educations"] = educations

        # Certifications
        certifications = []
        try:
            for cert in getattr(self, "certifications", []) or []:
                certifications.append(cert.to_export_dict())
        except Exception:
            pass
        context["certifications"] = certifications

        # Skills
        skills = []
        try:
            for skill in getattr(self, "skills", []) or []:
                skill_value = skill.to_export_value()
                if skill_value:
                    skills.append(skill_value)
        except Exception:
            pass
        context["skills"] = skills

        return context
