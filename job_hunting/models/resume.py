from django.conf import settings
from django.db import models
from .base import GetMixin
from .nanoid_pk import NanoIDModel


# Canonical section order — the historical SE-flavored sequence. Used as
# the fallback whenever a resume has neither an explicit section_order nor
# a profession we have an archetype default for.
CANONICAL_SECTION_ORDER = [
    "summary", "skills", "experience", "projects", "education", "certifications",
]

# Per-archetype defaults. Maps Resume.profession → ordered section list.
# Archetypes not listed here fall back to CANONICAL_SECTION_ORDER. The
# split is mostly skills-first (SE/BI: technical credentials matter more
# than narrative) vs experience-first (PM/PR/Marketing: track record and
# storytelling lead).
SECTION_ORDER_DEFAULTS = {
    "Software Engineering": list(CANONICAL_SECTION_ORDER),
    "Data / BI": list(CANONICAL_SECTION_ORDER),
    "Product Management": [
        "summary", "experience", "projects", "skills", "education", "certifications",
    ],
    "PR / Communications": [
        "summary", "experience", "projects", "skills", "education", "certifications",
    ],
    "Marketing": [
        "summary", "experience", "projects", "skills", "education", "certifications",
    ],
}


class Resume(GetMixin, NanoIDModel):
    # ``id`` is the 10-char NanoID string PK from NanoIDModel (CC-77 #79
    # true PK swap). Nine FKs reference resume(id): six CASCADE join tables
    # (resume_skill/resume_summaries/resume_certification/resume_education/
    # resume_experience/resume_project, resume_skill carrying a
    # unique_together(resume, skill)) plus score/cover_letter/job_application
    # (all SET_NULL, nullable). score additionally carries two named
    # composite UNIQUEs on resume_id rebuilt on the NanoID values.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resumes",
    )
    # Human-readable "where did this come from" marker: the original upload
    # filename. Retained for back-compat (serializer + duplicate export read
    # it); NOT the durable blob — see ``file`` below.
    file_path = models.CharField(max_length=500, null=True, blank=True)
    # CC-204 — the durable uploaded resume blob, stored via ``default_storage``
    # (Wasabi S3 in prod, local FileSystemStorage on self-host). The ingest
    # view saves the upload here and enqueues only ``resume_id``; the
    # resume_parse_job worker reads the bytes back from storage. Nullable +
    # blank because pre-CC-204 rows (and manually-created resumes) have none.
    file = models.FileField(
        upload_to="resumes/%Y/%m/", max_length=500, null=True, blank=True
    )
    title = models.CharField(max_length=255, null=True, blank=True)
    name = models.CharField(max_length=255, null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
    favorite = models.BooleanField(default=False)
    status = models.CharField(max_length=20, null=True, blank=True)
    # Free-form career archetype — drives the audience-aware extraction
    # prompt and section ordering. Frontend suggests canonical values
    # (Software Engineering / Product Management / Data BI / PR
    # Communications / Marketing / Sales / Operations / Design / Finance /
    # Other) but the column accepts any string the user picks.
    profession = models.CharField(max_length=64, null=True, blank=True)
    # Explicit per-resume override for section ordering. JSON list of
    # section keys (e.g. ["summary", "experience", "skills", ...]).
    # NULL means "use the archetype default for self.profession, or fall
    # back to CANONICAL_SECTION_ORDER".
    section_order = models.JSONField(null=True, blank=True)

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
    def skills_grouped(self):
        """Skills bucketed by skill_type in first-seen order. Skills with
        a falsy skill_type land in 'Other'. Templates that need to render
        every category — without baking in a fixed taxonomy — iterate this.
        """
        from collections import OrderedDict

        groups: "OrderedDict[str, list]" = OrderedDict()
        for skill in self._get_django_skills():
            key = skill.skill_type if (skill.skill_type and skill.skill_type.strip()) else "Other"
            groups.setdefault(key, []).append(skill)
        return groups

    @property
    def skills(self):
        return self._get_django_skills()

    @property
    def effective_section_order(self) -> list[str]:
        """Return the section sequence templates should iterate.

        Resolution order: explicit per-resume section_order → archetype
        default for self.profession → CANONICAL_SECTION_ORDER. Always
        returns a fresh list so callers can mutate without affecting the
        defaults.
        """
        if isinstance(self.section_order, list) and self.section_order:
            return list(self.section_order)
        if self.profession and self.profession in SECTION_ORDER_DEFAULTS:
            return list(SECTION_ORDER_DEFAULTS[self.profession])
        return list(CANONICAL_SECTION_ORDER)

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
