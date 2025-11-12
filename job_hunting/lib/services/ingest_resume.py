from pydantic import BaseModel, field_validator

from typing import Optional
from pydantic import Field
from pydantic_ai import Agent
from enum import Enum

import re
import os
import tempfile
from job_hunting.lib.parsers.docx_parser import DocxParser
from datetime import date
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.ollama import OllamaProvider
from pydantic_ai.models.openai import OpenAIResponsesModel
from job_hunting.lib.models.experience import Experience
from job_hunting.lib.models.experience_description import ExperienceDescription
from job_hunting.lib.models.education import Education
from job_hunting.lib.models.project import Project
from job_hunting.lib.models.project_description import ProjectDescription
from job_hunting.lib.models.certification import Certification
from job_hunting.lib.models.resume import Resume
from job_hunting.lib.models.resume_experience import ResumeExperience
from job_hunting.lib.models.resume_education import ResumeEducation
from job_hunting.lib.models.resume_certification import ResumeCertification
from job_hunting.lib.models.resume_skill import ResumeSkill
from job_hunting.lib.models.summary import Summary
from job_hunting.lib.models.resume_summary import ResumeSummaries
from job_hunting.lib.models.skill import Skill
from job_hunting.lib.models.company import Company
from job_hunting.lib.models.description import Description


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


class ExperienceOut(BaseModel):
    title: Optional[str] = None
    company: CompanyOut
    start_date: Optional[str] = None  # Expect "YYYY-MM" or "YYYY" or "present"
    end_date: Optional[str] = None  # Same
    location: Optional[str] = None
    bullets: list[str] = Field(default_factory=list)


class EducationOut(BaseModel):
    institution: str
    degree: Optional[str] = None
    major: Optional[str] = None
    minor: Optional[str] = None
    issue_date: Optional[str] = None


class ProjectOut(BaseModel):
    name: str
    role: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    bullets: list[str] = Field(default_factory=list)
    tech: list[str] = Field(default_factory=list)


class CertificationsOut(BaseModel):
    title: str
    issuer: Optional[str] = None
    issue_date: Optional[str] = None
    content: str = Field(...)


class SummaryOut(BaseModel):
    content: str


class ParsedResume(BaseModel):
    summary: Optional[SummaryOut] = None
    skills: list[SkillOut] = Field(default_factory=list)
    experiences: list[ExperienceOut] = Field(default_factory=list)
    education: list[EducationOut] = Field(default_factory=list)
    projects: list[ProjectOut] = Field(default_factory=list)
    certifications: list[CertificationsOut] = Field(default_factory=list)
    name: str
    phone: Optional[str]
    email: Optional[str]
    title: str


class IngestResume:
    def __init__(self, user=None, resume=None, agent=None):
        """
        Keyword Arguments:
        user   -- (default None) the user who is submitting the resume
        resume -- (default None) the resume they are submitting docx???
        agent  -- (default None) optional agent to pass in
        """
        self.user = user
        self.resume = resume  # what is this?
        # Defer agent creation until process() to avoid requiring external API keys during tests
        self.agent = agent
        self.db_resume = None

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

    def process(self):
        resume_md = self.extract_text_from_docx(self.resume)
        if self.agent is None:
            self.agent = self.get_agent()
        result = self.agent.run_sync(resume_md)
        parsed_resume = result.output

        resume = Resume(name=parsed_resume.name, title=parsed_resume.title)
        self.db_resume = resume
        
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
                summary, _ = Summary.first_or_create(content=content)
                summary.save()
                # Create join record linking resume and summary and mark as active
                rs = ResumeSummaries(
                    resume_id=self.db_resume.id, summary_id=summary.id, active=True
                )
                rs.save()
                # Ensure only one active summary per resume
                # ResumeSummaries.ensure_single_active_for_resume(resume.id)

        print("Creating experiences...")
        for exp_data in parsed_resume.experiences:
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

            # Ensure a Company record exists (Company.name is non-nullable).
            company, _ = Company.first_or_create(
                name=company_name, defaults={"display_name": company_display}
            )

            print(f"company: {company.name}")
            experience, _ = Experience.first_or_create(
                title=exp_data.title,
                company_id=company.id if company else None,
                start_date=self.parse_date(exp_data.start_date),
                end_date=self.parse_date(exp_data.end_date),
                location=exp_data.location,
            )

            # Ensure company_id is set (experience.company_id is non-nullable in the model)
            experience.company = company
            experience.resume = self.db_resume
            print("*" * 88)
            ResumeExperience.first_or_create(
                resume_id=self.db_resume.id, experience_id=experience.id
            )
            print("*" * 88)
            experience.save()

            # Create experience descriptions
            for bullet in exp_data.bullets:
                print(bullet)
                desc, _ = Description.first_or_create(content=bullet)
                # Link description to experience
                # This assumes ExperienceDescription is the linking table
                ExperienceDescription.first_or_create(
                    experience_id=experience.id, description_id=desc.id
                )

        print("Creating education...")
        for edu_data in parsed_resume.education:
            education = Education(
                institution=edu_data.institution,
                degree=edu_data.degree,
                major=edu_data.major,
                issue_date=self.parse_date(edu_data.issue_date),
            )
            education.save()
            ResumeEducation.first_or_create(resume=self.db_resume, education=education)

        print("Creating projects...")
        for proj_data in parsed_resume.projects:
            project = Project(
                name=proj_data.name,
                role=proj_data.role,
                start_date=self.parse_date(proj_data.start_date),
                end_date=self.parse_date(proj_data.end_date),
            )
            project.save()

            # Create project descriptions
            for bullet in proj_data.bullets:
                desc = Description(content=bullet)
                desc.save()
                # Link description to project
                # This assumes ProjectDescription is the linking table
                ProjectDescription.first_or_create(
                    project_id=project.id, description_id=desc.id
                )

        print("Creating certifications...")
        for cert_data in parsed_resume.certifications:
            certification = Certification(
                title=cert_data.title,
                issuer=cert_data.issuer,
                issue_date=self.parse_date(cert_data.issue_date),
                content=cert_data.content,
            )
            certification.save()
            ResumeCertification.first_or_create(
                certification=certification, resume=self.db_resume
            )

        print("Creating skills...")
        for skill_out in parsed_resume.skills:
            skill_model, _ = Skill.first_or_create(
                text=skill_out.text,
                defaults={
                    "skill_type": (skill_out.tag.value if skill_out.tag else None)
                },
            )
            try:
                ResumeSkill.first_or_create(
                    resume_id=self.db_resume.id, skill_id=skill_model.id
                )
            except Exception as e:
                print(e)
                breakpoint()

        print("Resume data saved successfully!")
        print(result.usage())
        # > RunUsage(input_tokens=57, output_tokens=8, requests=1)
        
        return self.db_resume

    def get_agent(self):
        # Prefer OpenAI if OPENAI_API_KEY is set; otherwise fall back to local Ollama.
        try:
            if os.getenv("OPENAI_API_KEY"):
                openai_model = OpenAIResponsesModel("gpt-5")
                return Agent(openai_model, output_type=ParsedResume)
        except Exception:
            # Fall back to Ollama if OpenAI model initialization fails for any reason
            pass

        ollama_model = OpenAIChatModel(
            model_name="qwen3-coder",
            provider=OllamaProvider(base_url="http://localhost:11434/v1"),
        )
        return Agent(ollama_model, output_type=ParsedResume)

    def parse_date(self, value: Optional[str]) -> Optional[date]:
        """
        Parse date strings like 'YYYY', 'YYYY-MM', 'YYYY-MM-DD'.
        Treat 'present', 'now', 'current' as None (open-ended).
        Returns a datetime.date or None.
        """
        if not value:
            return None
        v = str(value).strip()
        if v.lower() in ("present", "now", "current"):
            return None
        m = re.match(r"^(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?$", v)
        if m:
            year = int(m.group(1))
            month = int(m.group(2)) if m.group(2) else 1
            day = int(m.group(3)) if m.group(3) else 1
            try:
                return date(year, month, day)
            except ValueError:
                return None
        return None
