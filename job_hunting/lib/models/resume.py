from typing import Optional
from sqlalchemy import Column, Integer, Text, ForeignKey, String, Boolean
from sqlalchemy.orm import relationship
from .base import BaseModel, Base


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

    def _get_django_skills(self):
        from job_hunting.models import Skill as DjangoSkill
        from .resume_skill import ResumeSkill
        session = self.__class__.get_session()
        rs_links = session.query(ResumeSkill).filter_by(resume_id=self.id).all()
        skill_ids = [rs.skill_id for rs in rs_links]
        return list(DjangoSkill.objects.filter(pk__in=skill_ids))

    @property
    def language_skills(self):
        return self.skills_by_type("Language")

    @property
    def database_skills(self):
        return self.skills_by_type("Database")

    @property
    def framework_skills(self):
        return self.skills_by_type("Framework")

    @property
    def tool_skills(self):
        return self.skills_by_type("Tools/Platform")

    @property
    def security_skills(self):
        return self.skills_by_type("Security")

    def skills_by_type(self, skill_type):
        return [s for s in self._get_django_skills() if s.skill_type == skill_type]

    @property
    def active_summary(self):
        """Return the linked Django Summary where ResumeSummaries.active == True if present;
        otherwise the first linked summary; otherwise None."""
        from job_hunting.models import Summary as DjangoSummary
        try:
            links = getattr(self, "resume_summaries", []) or []
            active_link = next((l for l in links if getattr(l, "active", False)), None)
            if active_link and active_link.summary_id:
                return DjangoSummary.objects.filter(pk=active_link.summary_id).first()
            first_link = next(iter(links), None)
            if first_link and first_link.summary_id:
                return DjangoSummary.objects.filter(pk=first_link.summary_id).first()
        except Exception:
            pass
        return None

    def active_summary_content(self) -> str:
        """Return content from active_summary or empty string."""
        summary = self.active_summary
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
            from job_hunting.models import Education as DjangoEdu
            from .resume_education import ResumeEducation
            session = self.__class__.get_session()
            re_links = session.query(ResumeEducation).filter_by(resume_id=self.id).all()
            edu_ids = [re.education_id for re in re_links]
            for edu in DjangoEdu.objects.filter(pk__in=edu_ids):
                educations.append(edu.to_export_dict())
        except Exception:
            pass
        context["educations"] = educations

        # Certifications
        certifications = []
        try:
            from job_hunting.models import Certification as DjangoCert
            from .resume_certification import ResumeCertification
            session = self.__class__.get_session()
            rc_links = session.query(ResumeCertification).filter_by(resume_id=self.id).all()
            cert_ids = [rc.certification_id for rc in rc_links]
            for cert in DjangoCert.objects.filter(pk__in=cert_ids):
                certifications.append(cert.to_export_dict())
        except Exception:
            pass
        context["certifications"] = certifications

        # Skills
        skills = []
        try:
            for skill in self._get_django_skills():
                skill_value = skill.to_export_value()
                if skill_value:
                    skills.append(skill_value)
        except Exception:
            pass
        context["skills"] = skills

        return context
