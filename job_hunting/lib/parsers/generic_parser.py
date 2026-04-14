import os
import threading
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from pydantic import BaseModel, Field, field_validator
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.ollama import OllamaProvider

from job_hunting.lib.scrapers.html_cleaner import clean_html_to_markdown
from job_hunting.lib.services.prompt_utils import write_prompt_to_file
from job_hunting.models import Company, JobPost, Scrape


class ParsedJobData(BaseModel):
    """Pydantic model for structured extraction from scraped job content."""

    title: str = Field(..., min_length=1, max_length=500, description="Job title")
    company_name: str = Field(..., min_length=1, max_length=200, description="Company name")
    company_display_name: Optional[str] = Field(None, max_length=200, description="Company display name if different from name")
    description: Optional[str] = Field(None, description="Full job description / responsibilities / qualifications")
    posted_date: Optional[datetime] = Field(None, description="Date the job was posted")
    extraction_date: Optional[datetime] = Field(None, description="Date the data was extracted")
    salary_min: Optional[float] = Field(None, description="Minimum annual salary in USD (e.g. 175000 for $175K)")
    salary_max: Optional[float] = Field(None, description="Maximum annual salary in USD (e.g. 205000 for $205K)")
    location: Optional[str] = Field(None, max_length=255, description="Job location (city, state, country)")
    remote: Optional[bool] = Field(None, description="True if the role is remote or hybrid-remote")
    link: Optional[str] = Field(None, max_length=1000, description="Canonical URL / apply link for the job posting")

    @field_validator("title")
    @classmethod
    def validate_title(cls, v):
        if not v or not v.strip():
            raise ValueError("Job title cannot be empty")
        return v.strip()

    @field_validator("company_name")
    @classmethod
    def validate_company_name(cls, v):
        if not v or not v.strip():
            raise ValueError("Company name cannot be empty")
        return v.strip()

    @field_validator("company_display_name", "description", "location", "link")
    @classmethod
    def strip_optional_str(cls, v):
        if v is not None:
            return v.strip() if v.strip() else None
        return v


def _to_decimal(value) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


class GenericParser:
    def __init__(self, client=None):
        self.client = client
        self.agent = None

    def parse(self, scrape: Scrape, user=None):
        validated_data = self.analyze_with_ai(scrape)
        self.process_evaluation(scrape, validated_data, user=user)

    def get_agent(self):
        if self.agent:
            return self.agent

        if os.getenv("OPENAI_API_KEY"):
            try:
                openai_model = OpenAIResponsesModel("gpt-4o")
                return Agent(openai_model, output_type=ParsedJobData)
            except Exception:
                pass

        ollama_model = OpenAIChatModel(
            model_name="qwen3-coder",
            provider=OllamaProvider(base_url="http://localhost:11434/v1"),
        )
        return Agent(ollama_model, output_type=ParsedJobData)

    def process_evaluation(self, scrape: Scrape, validated_data: ParsedJobData, user=None):
        # Find or create company
        company, _ = Company.objects.get_or_create(
            name=validated_data.company_name,
            defaults={"display_name": validated_data.company_display_name},
        )

        job_defaults = {}
        if user:
            job_defaults["created_by"] = user
        if validated_data.description:
            job_defaults["description"] = validated_data.description
        if validated_data.posted_date:
            job_defaults["posted_date"] = validated_data.posted_date
        if validated_data.extraction_date:
            job_defaults["extraction_date"] = validated_data.extraction_date
        if validated_data.salary_min is not None:
            job_defaults["salary_min"] = _to_decimal(validated_data.salary_min)
        if validated_data.salary_max is not None:
            job_defaults["salary_max"] = _to_decimal(validated_data.salary_max)
        if validated_data.location:
            job_defaults["location"] = validated_data.location
        if validated_data.remote is not None:
            job_defaults["remote"] = validated_data.remote

        # Use the scrape URL as the canonical link — it's the known-good source.
        # The LLM-extracted link may be an apply URL, redirect, or null.
        link = scrape.url or validated_data.link

        # Prefer link-based lookup since link is unique
        job = None
        if link:
            job = JobPost.objects.filter(link=link).first()
        if job is None:
            job, _ = JobPost.objects.get_or_create(
                title=validated_data.title,
                company=company,
                defaults={**job_defaults, "link": link},
            )
        else:
            # Update fields that may have been missing on a prior pass
            update_fields = []
            for field, value in job_defaults.items():
                if value is not None and getattr(job, field) is None:
                    setattr(job, field, value)
                    update_fields.append(field)
            if update_fields:
                job.save(update_fields=update_fields)

        # Link scrape → job_post and company
        update_fields = []
        if not scrape.job_post_id:
            scrape.job_post_id = job.id
            update_fields.append("job_post_id")
        if not scrape.company_id and job.company_id:
            scrape.company_id = job.company_id
            update_fields.append("company_id")
        if update_fields:
            scrape.save(update_fields=update_fields)

    def analyze_with_ai(self, scrape: Scrape) -> ParsedJobData:
        content = scrape.job_content or ""
        if not content and scrape.html:
            content = clean_html_to_markdown(scrape.html)

        prompt = f"""Extract job posting information from the content below and return structured data.

Fields to extract:
- title: job title
- company_name: company name (canonical, e.g. "Nav Technologies, Inc.")
- company_display_name: shorter display name if different (e.g. "Nav")
- description: full description including responsibilities and qualifications
- posted_date: ISO date the job was posted (null if unknown)
- extraction_date: today's date/time
- salary_min: minimum annual salary as a plain number in USD (e.g. 175000 for $175K/yr; null if not stated)
- salary_max: maximum annual salary as a plain number in USD (null if not stated)
- location: city/state/country (e.g. "United States" or "Austin, TX")
- remote: true if role is remote or hybrid, false if fully on-site, null if unknown
- link: the job application or posting URL if present (null otherwise)

Content:
{content}
"""

        write_prompt_to_file(
            prompt,
            kind="job_parser",
            identifiers={
                "scrape_id": scrape.id,
                "job_post_id": getattr(scrape, "job_post_id", None),
            },
        )

        if self.agent is None:
            self.agent = self.get_agent()

        result = self.agent.run_sync(prompt)
        return result.output


def extract_job_from_scrape(scrape: Scrape) -> None:
    """
    Fire-and-forget: parse job_content on a completed scrape and create/update
    the JobPost and Company records.  Safe to call even if already extracted —
    bails out if job_post_id is already set.
    """
    if not (scrape.job_content and scrape.status == "completed" and not scrape.job_post_id):
        return

    scrape_id = scrape.id

    def _run():
        try:
            from job_hunting.models.scrape import Scrape as ScrapeModel
            s = ScrapeModel.objects.filter(pk=scrape_id).first()
            if not s or s.job_post_id:
                return
            parser = GenericParser()
            parser.parse(s)
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "extract_job_from_scrape failed scrape_id=%s", scrape_id
            )

    threading.Thread(target=_run, daemon=True).start()
