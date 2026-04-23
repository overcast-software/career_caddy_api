from django.conf import settings
from django.db import models
from .base import GetMixin


class Resume(GetMixin, models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resumes",
    )
    file_path = models.CharField(max_length=500, null=True, blank=True)
    title = models.CharField(max_length=255, null=True, blank=True)
    name = models.CharField(max_length=255, null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
    favorite = models.BooleanField(default=False)
    status = models.CharField(max_length=20, null=True, blank=True)

    class Meta:
        db_table = "resume"

    def skills_by_type(self, skill_type):
        return [s for s in self._get_django_skills() if s.skill_type == skill_type]

    def _get_django_skills(self):
        from job_hunting.models.skill import Skill
        from job_hunting.models.resume_skill import ResumeSkill

        skill_ids = list(ResumeSkill.objects.filter(resume_id=self.id).values_list("skill_id", flat=True))
        return list(Skill.objects.filter(pk__in=skill_ids))

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

    @property
    def skills(self):
        return self._get_django_skills()

    @property
    def experiences(self):
        from job_hunting.models.resume_experience import ResumeExperience
        from job_hunting.models.experience import Experience

        exp_ids = list(
            ResumeExperience.objects.filter(resume_id=self.id)
            .order_by("order")
            .values_list("experience_id", flat=True)
        )
        exp_map = {e.id: e for e in Experience.objects.filter(pk__in=exp_ids).select_related("company")}
        return [exp_map[eid] for eid in exp_ids if eid in exp_map]

    @property
    def projects(self):
        from job_hunting.models.resume_project import ResumeProject
        from job_hunting.models.project import Project

        proj_ids = list(
            ResumeProject.objects.filter(resume_id=self.id)
            .order_by("order")
            .values_list("project_id", flat=True)
        )
        return list(Project.objects.filter(pk__in=proj_ids))

    @property
    def certifications(self):
        from job_hunting.models.resume_certification import ResumeCertification
        from job_hunting.models.certification import Certification

        cert_ids = list(
            ResumeCertification.objects.filter(resume_id=self.id)
            .values_list("certification_id", flat=True)
        )
        return list(Certification.objects.filter(pk__in=cert_ids))

    @property
    def educations(self):
        from job_hunting.models.resume_education import ResumeEducation
        from job_hunting.models.education import Education

        edu_ids = list(
            ResumeEducation.objects.filter(resume_id=self.id)
            .values_list("education_id", flat=True)
        )
        return list(Education.objects.filter(pk__in=edu_ids))

    @property
    def user_phone(self):
        from job_hunting.models.profile import Profile

        try:
            prof = Profile.objects.filter(user_id=self.user_id).first()
            return prof.phone or "" if prof else ""
        except Exception:
            return ""

    @property
    def active_summary(self):
        from job_hunting.models.summary import Summary
        from job_hunting.models.resume_summary import ResumeSummary

        try:
            active_link = ResumeSummary.objects.filter(resume_id=self.id, active=True).first()
            if active_link:
                return Summary.objects.filter(pk=active_link.summary_id).first()
            first_link = ResumeSummary.objects.filter(resume_id=self.id).first()
            if first_link:
                return Summary.objects.filter(pk=first_link.summary_id).first()
        except Exception:
            pass
        return None

    def active_summary_content(self) -> str:
        s = self.active_summary
        return s.content if s else ""

    def to_export_context(self) -> dict:
        header = {
            "name": getattr(self.user, "get_full_name", lambda: "")() if self.user else "",
            "title": self.title or "",
            "phone": self.user_phone,
            "email": getattr(self.user, "email", "") if self.user else "",
        }
        return {
            "header": header,
            "HEADER": header,
            "summary": self.active_summary_content().strip(),
            "experiences": [e.to_export_dict() for e in self.experiences],
            "educations": [e.to_export_dict() for e in self.educations],
            "certifications": [c.to_export_dict() for c in self.certifications],
            "skills": [s.to_export_value() for s in self.skills if s.to_export_value()],
            "projects": [p.to_export_dict() for p in self.projects],
        }
