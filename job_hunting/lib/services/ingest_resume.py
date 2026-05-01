from pydantic import BaseModel, field_validator, model_validator

from typing import Optional
from pydantic import Field
from pydantic_ai import Agent
from enum import Enum

import calendar
import json
import logging
import re
import os
import tempfile
from job_hunting.lib.parsers.docx_parser import DocxParser
from datetime import date
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIModel
from pydantic_ai.providers.ollama import OllamaProvider
from job_hunting.models import Experience
from job_hunting.models import ExperienceDescription
from job_hunting.models import Education
from job_hunting.models import Project
from job_hunting.models import ProjectDescription
from job_hunting.models import Certification
from job_hunting.models import Resume
from job_hunting.models import ResumeExperience
from job_hunting.models import ResumeEducation
from job_hunting.models import ResumeCertification
from job_hunting.models import ResumeProject
from job_hunting.models import ResumeSkill
from job_hunting.models import ResumeSummary
from job_hunting.models import Skill
from job_hunting.models import Description, Summary, Company

logger = logging.getLogger(__name__)


RESUME_EXTRACTION_PROMPT = """You extract structured data from a resume.

Strict rules — violations degrade user trust:

1. ORDER. Preserve the order items appear on the resume. The first experience
   on the page is index 0. Bullets keep their source order within each
   experience. Never reorder bullets or experiences "chronologically" — you
   don't have enough context and the user's source is the source of truth.

2. BULLET ASSOCIATION. Every bullet belongs to the experience (or project)
   that immediately precedes it. Never move a bullet to a different
   experience. If you are uncertain which experience a bullet belongs to,
   attach it to the preceding one — do not guess, do not merge across
   sections.

3. VERBATIM BULLETS. Copy each bullet verbatim (trim surrounding whitespace
   only). Do not merge, split, paraphrase, or invent bullets.

4. DATES. Emit dates strictly as YYYY-MM, or YYYY when only a year is
   available, or the literal string "present" for an ongoing position. If
   the source is a range like "Jan 2020 – Mar 2022" or "2018 - 2020", split
   it into start_date and end_date. If a field is not present on the
   resume, omit it — do not fabricate.

5. COMPANY NAMES. Use exactly the company name as written. Do not expand
   acronyms, do not add "Inc." or "LLC" if the resume doesn't.

6. PAGE BREAKS. The text may contain "--- PAGE BREAK ---" markers. Treat
   these as invisible — experiences commonly cross page breaks. A bullet
   after a page break still belongs to the experience whose header was
   before the break.

7. OMIT UNKNOWNS. When a field is not on the resume, leave it unset. Do
   not invent summaries, titles, or dates."""


# Accepts: "Jan", "January" → 1 ; etc. Case-insensitive.
_MONTH_TO_NUM: dict[str, int] = {
    **{m.lower(): i for i, m in enumerate(calendar.month_abbr) if m},
    **{m.lower(): i for i, m in enumerate(calendar.month_name) if m},
    # Common non-standard abbreviation
    "sept": 9,
}


# Range separator — must not match the plain hyphen inside ISO dates like
# "2020-05-15" or "12-2020". Spaces around a hyphen DO indicate a range,
# and en-/em-dash always indicates one.
_RANGE_SEPARATORS = re.compile(
    r"(?:\s+to\s+|\s+-\s+|\s*[–—]\s*)", re.IGNORECASE
)


def _canonicalize_date_string(value: Optional[str]) -> Optional[str]:
    """
    Normalize a single date token to 'YYYY-MM' / 'YYYY' / 'present' / None.

    Accepts (case-insensitive, stripped):
      2020                    → "2020"
      2020-01 / 2020-1        → "2020-01"
      2020-01-15              → "2020-01"
      Jan 2020 / January 2020 → "2020-01"
      01/2020 / 1-2020        → "2020-01"
      Present / Now / Current → "present"

    If value contains a range separator ("2018 - 2020"), returns the
    canonicalized first half only. Callers can detect the range elsewhere.

    Returns None for empty input or unrecognized formats.
    """
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    if v.lower() in ("present", "now", "current", "ongoing"):
        return "present"

    # If it's a range, take the first half.
    parts = _RANGE_SEPARATORS.split(v, maxsplit=1)
    if len(parts) == 2 and parts[1].strip():
        first = _canonicalize_date_string(parts[0])
        return first

    # YYYY / YYYY-MM / YYYY-MM-DD
    m = re.match(r"^(\d{4})(?:[-/](\d{1,2})(?:[-/](\d{1,2}))?)?$", v)
    if m:
        year = int(m.group(1))
        if m.group(2):
            month = int(m.group(2))
            if 1 <= month <= 12:
                return f"{year:04d}-{month:02d}"
            return None
        return f"{year:04d}"

    # MM/YYYY or M-YYYY
    m = re.match(r"^(\d{1,2})[-/](\d{4})$", v)
    if m:
        month = int(m.group(1))
        year = int(m.group(2))
        if 1 <= month <= 12:
            return f"{year:04d}-{month:02d}"

    # Month-name YYYY, with optional day
    m = re.match(
        r"^([A-Za-z]{3,9})\.?\s+(?:(\d{1,2}),?\s+)?(\d{4})$", v
    )
    if m:
        month_num = _MONTH_TO_NUM.get(m.group(1).lower())
        if month_num:
            return f"{int(m.group(3)):04d}-{month_num:02d}"

    # YYYY Month-name
    m = re.match(r"^(\d{4})\s+([A-Za-z]{3,9})\.?$", v)
    if m:
        month_num = _MONTH_TO_NUM.get(m.group(2).lower())
        if month_num:
            return f"{int(m.group(1)):04d}-{month_num:02d}"

    return None


def _split_date_range(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """
    Split a string that may contain a date range into (start, end).
    Canonicalizes each half. Returns (None, None) for empty input.
    """
    if value is None:
        return (None, None)
    v = str(value).strip()
    if not v:
        return (None, None)
    parts = _RANGE_SEPARATORS.split(v, maxsplit=1)
    if len(parts) == 2 and parts[1].strip():
        return (_canonicalize_date_string(parts[0]), _canonicalize_date_string(parts[1]))
    return (_canonicalize_date_string(v), None)


class SkillTag(Enum):
    FRAMEWORK = "Framework"
    DATABASE = "Database"
    TOOL_PLATFORM = "Tool/Platform"
    SECURITY = "Security"
    LANGUAGE = "Language"


class CompanyOut(BaseModel):
    name: str
    display_name: Optional[str] = None


class SkillOut(BaseModel):
    text: str
    tag: Optional[SkillTag]

    @field_validator("tag", mode="before")
    @classmethod
    def normalize_tag(cls, value):
        if value is None:
            return None

        s = str(value).strip().lower()

        # Map variants to canonical enum values
        if s in {"framework", "frameworks"}:
            return SkillTag.FRAMEWORK
        elif s in {"database", "databases"}:
            return SkillTag.DATABASE
        elif s in {
            "tool/platform",
            "tool platform",
            "tools/platforms",
            "tools",
            "platform",
            "platforms",
        }:
            return SkillTag.TOOL_PLATFORM
        elif s in {"security"}:
            return SkillTag.SECURITY
        elif s in {"language", "languages"}:
            return SkillTag.LANGUAGE
        else:
            # Try to match enum values directly
            for tag in SkillTag:
                if s == tag.value.lower():
                    return tag

            raise ValueError(
                f"Invalid skill tag: {value}. Must be one of: {[tag.value for tag in SkillTag]}"
            )


def _normalize_date_field(cls, value):  # noqa: ARG001
    if value is None:
        return None
    return _canonicalize_date_string(value)


class ExperienceOut(BaseModel):
    title: Optional[str] = None
    company: CompanyOut
    summary: Optional[str]
    start_date: Optional[str] = None  # Canonicalized to "YYYY-MM" / "YYYY" / "present"
    end_date: Optional[str] = None
    location: Optional[str] = None
    bullets: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _split_range_when_end_missing(cls, data):
        # If the LLM packed a whole range into start_date and left end_date
        # empty, recover both halves before per-field canonicalization runs.
        if isinstance(data, dict):
            start = data.get("start_date")
            end = data.get("end_date")
            if start and not end:
                s, e = _split_date_range(start)
                if e:
                    data["start_date"] = s
                    data["end_date"] = e
        return data

    _norm_start = field_validator("start_date", mode="before")(_normalize_date_field)
    _norm_end = field_validator("end_date", mode="before")(_normalize_date_field)


class EducationOut(BaseModel):
    institution: str
    degree: Optional[str] = None
    major: Optional[str] = None
    minor: Optional[str] = None
    issue_date: Optional[str] = None

    _norm_issue = field_validator("issue_date", mode="before")(_normalize_date_field)


class ProjectOut(BaseModel):
    title: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    bullets: list[str] = Field(default_factory=list)
    tech: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _split_range_when_end_missing(cls, data):
        if isinstance(data, dict):
            start = data.get("start_date")
            end = data.get("end_date")
            if start and not end:
                s, e = _split_date_range(start)
                if e:
                    data["start_date"] = s
                    data["end_date"] = e
        return data

    _norm_start = field_validator("start_date", mode="before")(_normalize_date_field)
    _norm_end = field_validator("end_date", mode="before")(_normalize_date_field)


class CertificationsOut(BaseModel):
    title: str
    issuer: Optional[str] = None
    issue_date: Optional[str] = None
    content: str = Field(...)

    _norm_issue = field_validator("issue_date", mode="before")(_normalize_date_field)


class SummaryOut(BaseModel):
    content: str


class ParsedResume(BaseModel):
    summary: Optional[SummaryOut] = None
    skills: list[SkillOut] = Field(default_factory=list)
    experiences: list[ExperienceOut] = Field(default_factory=list)
    education: Optional[list[EducationOut]] = Field(default_factory=list)
    projects: Optional[list[ProjectOut]] = Field(default_factory=list)
    certifications: list[CertificationsOut] = Field(default_factory=list)
    name: str
    phone: Optional[str]
    email: Optional[str]
    title: str


class IngestResume:
    def __init__(self, user=None, resume=None, resume_name=None, agent=None, db_resume=None):
        """
        Keyword Arguments:
        user        -- the user who is submitting the resume
        resume      -- file blob (bytes) or file path of the docx
        resume_name -- display name for the resume within the app
        agent       -- optional pydantic-ai Agent to use
        db_resume   -- optional pre-created Resume record (for async/polling pattern)
        """

        self.user = user
        self.resume = resume
        self.resume_name = resume_name
        self.agent = agent
        self.db_resume = db_resume

    def _resolve_user_id(self, user) -> Optional[int]:
        """
        Extract a user id from either a Django user or an SA user.

        Args:
            user: User object (Django or SQLAlchemy)

        Returns:
            int: User ID if available, None otherwise
        """
        if user is None:
            return None

        # Try to get id or pk attribute
        user_id = getattr(user, "id", None) or getattr(user, "pk", None)

        if user_id is not None:
            try:
                return int(user_id)
            except (ValueError, TypeError):
                pass

        return None

    def extract_text_from_docx(self, source):
        """
        Extract text from a docx file.

        Args:
            source: Either a file path (str) or binary blob (bytes-like object)

        Returns:
            str: Markdown text extracted from the docx
        """
        temp_file_path = None

        try:
            # Determine if source is a path or blob
            if isinstance(source, str):
                # Path-based processing (existing behavior)
                if not os.path.exists(source):
                    raise ValueError(f"File not found: {source}")
                if not source.lower().endswith(".docx"):
                    raise ValueError("Only .docx files are supported")
                docx_path = source
            else:
                # Blob-based processing
                if not hasattr(source, "read"):
                    # Assume it's bytes-like
                    blob_data = source
                else:
                    # File-like object
                    blob_data = source.read()

                # Create temporary file
                temp_file = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
                temp_file_path = temp_file.name
                temp_file.write(blob_data)
                temp_file.close()
                docx_path = temp_file_path

            dxp = DocxParser(docx_path)

            # Save HTML (best-effort) - only for path-based to avoid disk artifacts for blobs
            if isinstance(source, str):
                try:
                    html = dxp.to_html().value
                    with open("resume.html", "w") as f:
                        f.write(html)
                except Exception:
                    html = ""

            # Get Markdown and return it
            try:
                md_text = dxp.to_markdown()
                # Only save to disk for path-based processing
                if isinstance(source, str):
                    with open("resume.md", "w") as f:
                        f.write(md_text)
            except Exception as e:
                raise RuntimeError(f"Failed to convert .docx to markdown: {e}") from e

            return md_text

        finally:
            # Clean up temporary file if created
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.unlink(temp_file_path)
                except Exception:
                    pass  # Best effort cleanup

    def extract_text_from_pdf(self, source):
        """
        Extract text from a PDF. Prefer pdfplumber (layout-aware, better on
        multi-column resumes); fall back to pypdf if pdfplumber errors.
        Pages are joined with an explicit "--- PAGE BREAK ---" marker so the
        LLM can treat it as invisible rather than a content boundary.
        """
        import io

        blob_data: Optional[bytes] = None
        path: Optional[str] = None

        if isinstance(source, str):
            if not os.path.exists(source):
                raise ValueError(f"File not found: {source}")
            path = source
        else:
            blob_data = source.read() if hasattr(source, "read") else source

        assert path is not None or blob_data is not None
        try:
            import pdfplumber

            opener = (
                pdfplumber.open(path)
                if path
                else pdfplumber.open(io.BytesIO(blob_data or b""))
            )
            with opener as pdf:
                pages = [page.extract_text() or "" for page in pdf.pages]
            return "\n\n--- PAGE BREAK ---\n\n".join(pages)
        except Exception:
            # Fallback: pypdf — lossier, but always installed.
            from pypdf import PdfReader

            reader = PdfReader(path) if path else PdfReader(io.BytesIO(blob_data or b""))
            try:
                pages = [page.extract_text() or "" for page in reader.pages]
            except Exception as e:
                raise RuntimeError(f"Failed to extract text from PDF: {e}") from e
            return "\n\n--- PAGE BREAK ---\n\n".join(pages)

    def _extract_text(self, source, resume_name=None):
        """Dispatch to the right extractor based on filename/path extension."""
        name = (
            source.lower() if isinstance(source, str)
            else (resume_name or "").lower()
        )
        if name.endswith(".pdf"):
            return self.extract_text_from_pdf(source)
        return self.extract_text_from_docx(source)

    def process(self):
        resume_md = self._extract_text(self.resume, self.resume_name)
        result = None
        if self.agent is None:
            self.agent = self.get_agent()
        result = self.agent.run_sync(resume_md)
        self._record_usage(result)
        output = result.output
        if isinstance(output, dict):
            parsed_resume = ParsedResume(**output)
        elif isinstance(output, str):
            parsed_resume = ParsedResume(**json.loads(output))
        else:
            parsed_resume = output
        if self.db_resume:
            self.db_resume.title = parsed_resume.title
            if not self.db_resume.name:
                self.db_resume.name = self.resume_name
        else:
            self.db_resume = Resume(name=self.resume_name, title=parsed_resume.title)

        from job_hunting.models import Profile as DjangoProfile

        prof = DjangoProfile.objects.filter(user_id=self.user.id).first()
        if prof is None:
            prof = DjangoProfile.objects.create(user_id=self.user.id)
        if parsed_resume.phone:
            prof.phone = parsed_resume.phone
            prof.save()

        # Set user_id instead of user relationship to avoid cross-ORM issues
        user_id = self._resolve_user_id(self.user)
        if user_id is not None:
            self.db_resume.user_id = user_id

        self.db_resume.save()
        # Create and save models
        print("Creating summary...")
        if parsed_resume.summary:
            # parsed_resume.summary may be a plain string or a SummaryOut pydantic model.
            if isinstance(parsed_resume.summary, str):
                content = parsed_resume.summary
            else:
                content = getattr(parsed_resume.summary, "content", None)
            if content:
                summary, _ = Summary.objects.get_or_create(content=content)
                # Create join record linking resume and summary and mark as active
                ResumeSummary.objects.get_or_create(
                    resume_id=self.db_resume.id,
                    summary_id=summary.id,
                    defaults={"active": True},
                )
                ResumeSummary.ensure_single_active_for_resume(self.db_resume.id)

        print("Creating experiences...")
        for exp_idx, exp_data in enumerate(parsed_resume.experiences or []):
            # exp_data.company may be a CompanyOut pydantic model, a dict, or a plain string.
            comp = getattr(exp_data, "company", None)
            company_name = None
            company_display = None

            if comp:
                if isinstance(comp, dict):
                    company_name = comp.get("name") or ""
                    company_display = comp.get("display_name")
                elif isinstance(comp, str):
                    company_name = comp or ""
                else:
                    company_name = getattr(comp, "name", None) or ""
                    company_display = getattr(comp, "display_name", None)

            # Company is shared across all users — get_or_create is correct here.
            company, _ = Company.objects.get_or_create(
                name=company_name, defaults={"display_name": company_display}
            )

            # Experience and its bullets belong to this resume — do NOT
            # get_or_create, or bullets from unrelated resumes can attach.
            experience = Experience.objects.create(
                title=exp_data.title,
                company_id=company.id if company else None,
                start_date=self.parse_date(exp_data.start_date),
                end_date=self.parse_date(exp_data.end_date),
                location=exp_data.location,
            )

            ResumeExperience.objects.create(
                resume_id=self.db_resume.id,
                experience_id=experience.id,
                order=exp_idx,
            )

            for bullet_idx, bullet in enumerate(exp_data.bullets or []):
                desc = Description.objects.create(content=bullet)
                ExperienceDescription.objects.create(
                    experience_id=experience.id,
                    description_id=desc.id,
                    order=bullet_idx,
                )

        print("Creating education...")
        for edu_data in (parsed_resume.education or []):
            education = Education.objects.create(
                institution=edu_data.institution,
                degree=edu_data.degree,
                major=edu_data.major,
                issue_date=self.parse_date(edu_data.issue_date),
            )
            ResumeEducation.objects.get_or_create(
                resume_id=self.db_resume.id, education_id=education.id
            )

        print("Creating projects...")
        for idx, proj_data in enumerate(parsed_resume.projects or []):
            project = Project(
                title=proj_data.title,
                start_date=self.parse_date(proj_data.start_date),
                end_date=self.parse_date(proj_data.end_date),
                user_id=user_id,
            )
            project.save()

            ResumeProject.objects.create(
                resume_id=self.db_resume.id,
                project_id=project.id,
                order=idx,
            )

            for bullet_idx, bullet in enumerate(proj_data.bullets or []):
                desc = Description.objects.create(content=bullet)
                ProjectDescription.objects.create(
                    project_id=project.id,
                    description_id=desc.id,
                    order=bullet_idx,
                )

        print("Creating certifications...")
        for cert_data in (parsed_resume.certifications or []):
            certification = Certification.objects.create(
                title=cert_data.title,
                issuer=cert_data.issuer,
                issue_date=self.parse_date(cert_data.issue_date),
                content=cert_data.content,
            )
            ResumeCertification.objects.get_or_create(
                certification_id=certification.id, resume_id=self.db_resume.id
            )

        print("Creating skills...")
        for skill_out in (parsed_resume.skills or []):
            skill_model, _ = Skill.objects.get_or_create(
                text=skill_out.text,
                defaults={
                    "skill_type": (skill_out.tag.value if skill_out.tag else None)
                },
            )
            try:
                ResumeSkill.objects.get_or_create(
                    resume_id=self.db_resume.id, skill_id=skill_model.id
                )
            except Exception as e:
                print(e)

        print("Resume data saved successfully!")
        print(result.usage())
        # > RunUsage(input_tokens=57, output_tokens=8, requests=1)

        return self.db_resume

    def get_agent(self):
        if self.agent:
            return self.agent

        model_spec = os.getenv("RESUME_INGEST_MODEL", "").strip()

        # Explicit model override: "provider:model_name"
        if model_spec:
            return self._agent_from_spec(model_spec)

        # Default: Anthropic > OpenAI > Ollama
        if os.getenv("ANTHROPIC_API_KEY"):
            from pydantic_ai.models.anthropic import AnthropicModel
            model = AnthropicModel("claude-sonnet-4-6")
            return Agent(
                model, output_type=ParsedResume, system_prompt=RESUME_EXTRACTION_PROMPT
            )

        if os.getenv("OPENAI_API_KEY"):
            try:
                # gpt-4o (not gpt-5) — gpt-5 access is gated on some
                # OpenAI tiers and silently 400s with model_not_found.
                # Override via RESUME_INGEST_MODEL=openai:gpt-5 if the
                # account has access.
                model = OpenAIModel("gpt-4o")
                return Agent(
                    model,
                    output_type=ParsedResume,
                    system_prompt=RESUME_EXTRACTION_PROMPT,
                )
            except Exception:
                pass

        ollama_base = os.getenv("OLLAMA_API_BASE", "http://localhost:11434/v1")
        ollama_model = OpenAIChatModel(
            model_name="qwen3-coder",
            provider=OllamaProvider(base_url=ollama_base),
        )
        return Agent(
            ollama_model,
            output_type=ParsedResume,
            system_prompt=RESUME_EXTRACTION_PROMPT,
        )

    def _get_model_name(self) -> str:
        """Return a 'provider:model' label for the Agent's active model.
        Mirrors JobPostExtractor._get_model_name so AiUsage rows are consistent."""
        if self.agent is None:
            return "unknown"
        model = getattr(self.agent, "model", None)
        if model is None:
            return "unknown"
        cls_name = type(model).__name__
        name = getattr(model, "model_name", None) or str(model)
        if cls_name == "AnthropicModel":
            return f"anthropic:{name}"
        if cls_name == "OpenAIResponsesModel":
            return f"openai:{name}"
        if cls_name == "OpenAIModel":
            return f"openai:{name}"
        if cls_name == "OpenAIChatModel":
            # Used in this file only for the Ollama fallback.
            return f"ollama:{name}"
        return str(model)

    def _record_usage(self, result) -> None:
        """Persist an AiUsage row for this ingest run. Errors are swallowed
        so a telemetry failure never breaks resume import."""
        try:
            from job_hunting.models.ai_usage import AiUsage
            from job_hunting.lib.pricing import estimate_cost

            usage = result.usage()
            request_tokens = getattr(usage, "request_tokens", 0) or 0
            response_tokens = getattr(usage, "response_tokens", 0) or 0
            total_tokens = getattr(usage, "total_tokens", 0) or 0
            request_count = getattr(usage, "requests", 1) or 1

            user_id = self._resolve_user_id(self.user)
            user = None
            if user_id is not None:
                from django.contrib.auth import get_user_model
                user = get_user_model().objects.filter(pk=user_id).first()
            if user is None:
                from django.contrib.auth import get_user_model
                user = (
                    get_user_model()
                    .objects.filter(is_staff=True)
                    .order_by("id")
                    .first()
                )
            if user is None:
                logger.warning("Skipping resume_importer usage — no user to attribute")
                return

            model_name = self._get_model_name()
            AiUsage.objects.create(
                user=user,
                agent_name="resume_importer",
                model_name=model_name,
                trigger="resume_import",
                request_tokens=request_tokens,
                response_tokens=response_tokens,
                total_tokens=total_tokens,
                request_count=request_count,
                estimated_cost_usd=estimate_cost(
                    model_name, request_tokens, response_tokens
                ),
            )
            logger.info(
                "Recorded resume_importer usage: model=%s tokens=%s/%s user_id=%s",
                model_name, request_tokens, response_tokens, user.id,
            )
        except Exception:
            logger.exception("Failed to record resume_importer usage")

    def _agent_from_spec(self, spec: str):
        """Parse 'provider:model_name' and return an Agent.

        Bare names (no ':') raise ValueError — RESUME_INGEST_MODEL must
        spell out the provider so the wrong-client misroute that bit
        the scrape graph (Tier2 'anthropic:claude-haiku-4-5' going to
        OpenAI's HTTP client, 2026-04-30) cannot recur here.
        """
        if ":" not in spec:
            raise ValueError(
                f"RESUME_INGEST_MODEL {spec!r} must use 'provider:model' "
                "form (e.g. 'openai:gpt-4o', 'anthropic:claude-sonnet-4-6')."
            )
        provider, model_name = spec.split(":", 1)

        if provider == "anthropic":
            from pydantic_ai.models.anthropic import AnthropicModel
            return Agent(
                AnthropicModel(model_name),
                output_type=ParsedResume,
                system_prompt=RESUME_EXTRACTION_PROMPT,
            )
        elif provider == "openai":
            return Agent(
                OpenAIModel(model_name),
                output_type=ParsedResume,
                system_prompt=RESUME_EXTRACTION_PROMPT,
            )
        elif provider == "ollama":
            ollama_base = os.getenv("OLLAMA_API_BASE", "http://localhost:11434/v1")
            model = OpenAIChatModel(
                model_name=model_name,
                provider=OllamaProvider(base_url=ollama_base),
            )
            return Agent(
                model,
                output_type=ParsedResume,
                system_prompt=RESUME_EXTRACTION_PROMPT,
            )
        else:
            raise ValueError(f"Unknown provider: {provider}")

    def parse_date(self, value: Optional[str]) -> Optional[date]:
        """
        Parse a date token to a datetime.date, or None for empty / 'present' /
        unrecognized. Accepts the canonical forms produced by
        _canonicalize_date_string plus the raw strings that function accepts.
        """
        canonical = _canonicalize_date_string(value)
        if not canonical or canonical == "present":
            return None
        m = re.match(r"^(\d{4})(?:-(\d{2}))?$", canonical)
        if not m:
            return None
        year = int(m.group(1))
        month = int(m.group(2)) if m.group(2) else 1
        try:
            return date(year, month, 1)
        except ValueError:
            return None
