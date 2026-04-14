import logging
import os
import threading
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from pydantic import BaseModel, Field, field_validator
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.ollama import OllamaProvider

from urllib.parse import urlparse

from job_hunting.lib.scrapers.html_cleaner import clean_html_to_markdown
from job_hunting.lib.services.prompt_utils import write_prompt_to_file
from job_hunting.models import Company, JobPost, Scrape

logger = logging.getLogger(__name__)


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


_DEFAULT_PARSER_MODEL = "gpt-4o"


class JobPostExtractor:
    def __init__(self, client=None):
        self.client = client
        self.agent = None

    def parse(self, scrape: Scrape, user=None):
        # Try Tier 0 (deterministic CSS extraction) first — $0 cost
        validated_data = self._try_tier0_extraction(scrape)
        if validated_data:
            logger.info("Tier 0 extraction succeeded for scrape %s", scrape.id)
        else:
            validated_data = self.analyze_with_ai(scrape)
        self.process_evaluation(scrape, validated_data, user=user)

    def _resolve_model_name(self) -> str:
        """Role-specific env var -> fallback env var -> default."""
        return (
            os.environ.get("JOB_PARSER_MODEL")
            or os.environ.get("CADDY_DEFAULT_MODEL")
            or _DEFAULT_PARSER_MODEL
        )

    def get_agent(self):
        if self.agent:
            return self.agent

        model_name = self._resolve_model_name()

        if model_name.startswith("ollama:"):
            ollama_model_name = model_name.split(":", 1)[1]
            model = OpenAIChatModel(
                model_name=ollama_model_name,
                provider=OllamaProvider(
                    base_url=os.environ.get("OLLAMA_API_BASE", "http://localhost:11434/v1")
                ),
            )
        else:
            model = OpenAIResponsesModel(model_name)

        self.agent = Agent(model, output_type=ParsedJobData)
        return self.agent

    def process_evaluation(self, scrape: Scrape, validated_data: ParsedJobData, user=None):
        # Find or create company — Company is a shared resource (no user scoping).
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
            if not job.company_id:
                job.company = company
                update_fields.append("company_id")
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

    def _get_model_name(self) -> str:
        if self.agent is None:
            return "unknown"
        model = getattr(self.agent, "model", None)
        if model is None:
            return "unknown"
        if isinstance(model, OpenAIResponsesModel):
            return f"openai:{model.model_name}"
        if isinstance(model, OpenAIChatModel):
            return f"ollama:{model.model_name}"
        return str(model)

    def _get_profile_hints(self, scrape: Scrape) -> str:
        """Look up ScrapeProfile for this scrape's hostname and return hint text."""
        try:
            from job_hunting.models import ScrapeProfile
            hostname = urlparse(scrape.url or "").hostname or ""
            if not hostname:
                return ""
            # Strip www. prefix for matching
            if hostname.startswith("www."):
                hostname = hostname[4:]
            profile = ScrapeProfile.objects.filter(
                hostname=hostname, enabled=True
            ).first()
            if not profile:
                return ""
            parts = []
            if profile.extraction_hints:
                parts.append(f"Previous extractions from this domain found: {profile.extraction_hints}")
            if profile.page_structure:
                parts.append(f"Page structure: {profile.page_structure}")
            return "\n".join(parts)
        except Exception:
            logger.debug("Could not load scrape profile hints", exc_info=True)
            return ""

    def _try_tier0_extraction(self, scrape: Scrape) -> Optional[ParsedJobData]:
        """Attempt deterministic extraction using CSS selectors from ScrapeProfile.

        Returns ParsedJobData if all required fields (title, company_name) are found,
        None otherwise (caller should fall back to LLM).
        """
        if not scrape.html:
            return None

        try:
            from bs4 import BeautifulSoup
            from job_hunting.models import ScrapeProfile

            hostname = urlparse(scrape.url or "").hostname or ""
            if hostname.startswith("www."):
                hostname = hostname[4:]
            if not hostname:
                return None

            profile = ScrapeProfile.objects.filter(
                hostname=hostname, enabled=True
            ).first()
            if not profile or not profile.css_selectors:
                return None
            if profile.preferred_tier not in ("auto", "0"):
                return None

            selectors = profile.css_selectors
            if not isinstance(selectors, dict):
                return None

            soup = BeautifulSoup(scrape.html, "html.parser")
            extracted = {}
            for field, selector in selectors.items():
                el = soup.select_one(selector)
                if el:
                    extracted[field] = el.get_text(strip=True)

            title = extracted.get("title", "")
            company = extracted.get("company_name", "") or extracted.get("company", "")
            if not title or not company:
                return None

            logger.info(
                "Tier 0 extraction for %s: title=%s, company=%s",
                hostname, title[:50], company[:50],
            )

            return ParsedJobData(
                title=title,
                company_name=company,
                description=extracted.get("description"),
                location=extracted.get("location"),
                remote=None,
                salary_min=None,
                salary_max=None,
                link=scrape.url,
                extraction_date=datetime.now(),
            )
        except Exception:
            logger.debug("Tier 0 extraction failed", exc_info=True)
            return None

    def analyze_with_ai(self, scrape: Scrape) -> ParsedJobData:
        content = scrape.job_content or ""
        if not content and scrape.html:
            content = clean_html_to_markdown(scrape.html)

        hints = self._get_profile_hints(scrape)
        hints_block = f"\n\nDomain hints (from previous successful extractions):\n{hints}\n" if hints else ""

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
{hints_block}
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
        self._record_usage(result, scrape)
        return result.output

    def _record_usage(self, result, scrape: Scrape) -> None:
        try:
            from job_hunting.models.ai_usage import AiUsage
            from job_hunting.lib.pricing import estimate_cost

            usage = result.usage()
            model_name = self._get_model_name()
            user = scrape.created_by or _get_genesis_user()

            request_tokens = usage.request_tokens or 0
            response_tokens = usage.response_tokens or 0

            AiUsage.objects.create(
                user=user,
                agent_name="job_post_extractor",
                model_name=model_name,
                trigger="scrape",
                request_tokens=request_tokens,
                response_tokens=response_tokens,
                total_tokens=usage.total_tokens or 0,
                request_count=1,
                estimated_cost_usd=estimate_cost(model_name, request_tokens, response_tokens),
            )
            logger.info(
                "Recorded parser usage: model=%s tokens=%s/%s scrape_id=%s",
                model_name, request_tokens, response_tokens, scrape.id,
            )
        except Exception:
            logger.exception("Failed to record parser usage for scrape_id=%s", scrape.id)


def _get_genesis_user():
    """Return the first staff user (genesis user) as a fallback for cost attribution."""
    from django.contrib.auth import get_user_model
    User = get_user_model()
    return User.objects.filter(is_staff=True).order_by("id").first()


def parse_scrape(scrape_id: int, user_id: int = None, sync: bool = False) -> None:
    """
    Single entry point for parsing a scrape into a JobPost + Company.

    Handles status transitions (extracting -> completed/failed), error logging,
    and user resolution.  Safe to call even if already extracted — bails out if
    job_post_id is already set.

    Args:
        scrape_id: PK of the Scrape record.
        user_id: PK of the user to attribute created records to.
                 Falls back to scrape.created_by if None.
        sync: If True, run inline (use when already in a background thread).
              If False, spawn a daemon thread.
    """

    def _run():
        from job_hunting.models.scrape import Scrape as ScrapeModel
        from job_hunting.lib.scraper import _log_scrape_status
        from django.contrib.auth import get_user_model

        try:
            scrape = ScrapeModel.objects.filter(pk=scrape_id).first()
            if not scrape:
                logger.warning("parse_scrape: scrape_id=%s not found", scrape_id)
                return
            if scrape.job_post_id:
                logger.info("parse_scrape: scrape_id=%s already has job_post, skipping", scrape_id)
                return
            if not (scrape.job_content or scrape.html):
                logger.warning("parse_scrape: scrape_id=%s has no content", scrape_id)
                return

            user = None
            if user_id:
                User = get_user_model()
                user = User.objects.filter(pk=user_id).first()
            if not user:
                user = scrape.created_by

            _log_scrape_status(scrape_id, "extracting")

            parser = JobPostExtractor()
            parser.parse(scrape, user=user)

            _log_scrape_status(scrape_id, "completed", note="Parsed successfully")

            # Auto-populate scrape profile for this domain
            try:
                _update_scrape_profile(scrape, user)
            except Exception:
                logger.debug("Failed to update scrape profile", exc_info=True)

        except Exception:
            logger.exception("parse_scrape failed scrape_id=%s", scrape_id)
            try:
                _log_scrape_status(scrape_id, "failed", note="Extraction failed")
            except Exception:
                pass

    if sync:
        _run()
    else:
        threading.Thread(target=_run, daemon=True).start()


def _update_scrape_profile(scrape, user=None):
    """Create or update ScrapeProfile for this scrape's hostname after successful extraction."""
    from django.utils import timezone
    from job_hunting.models import ScrapeProfile

    hostname = urlparse(scrape.url or "").hostname or ""
    if not hostname:
        return
    if hostname.startswith("www."):
        hostname = hostname[4:]

    content_len = len(scrape.job_content or "")

    profile, created = ScrapeProfile.objects.get_or_create(
        hostname=hostname,
        defaults={
            "requires_auth": False,
            "avg_content_length": content_len,
            "success_rate": 1.0,
            "scrape_count": 1,
            "last_success_at": timezone.now(),
            "created_by": user,
        },
    )

    if not created:
        profile.scrape_count += 1
        profile.last_success_at = timezone.now()
        # Rolling average of content length
        if profile.avg_content_length:
            profile.avg_content_length = int(
                (profile.avg_content_length * (profile.scrape_count - 1) + content_len)
                / profile.scrape_count
            )
        else:
            profile.avg_content_length = content_len
        # Update success rate (simple: successes / total)
        profile.success_rate = min(
            1.0,
            (profile.success_rate * (profile.scrape_count - 1) + 1.0)
            / profile.scrape_count,
        )
        profile.save()

    logger.info(
        "Scrape profile %s for %s (count=%d, rate=%.0f%%)",
        "created" if created else "updated",
        hostname,
        profile.scrape_count,
        profile.success_rate * 100,
    )

    # Generate extraction hints with a cheap LLM call (only on first scrape or if hints are empty)
    if not profile.extraction_hints:
        try:
            _generate_profile_hints(profile, scrape, user)
        except Exception:
            logger.debug("Failed to generate profile hints for %s", hostname, exc_info=True)


class ProfileHints(BaseModel):
    """Structured output for scrape profile hint generation."""
    extraction_hints: str = Field(
        description="2-3 sentences describing patterns that help extract key fields "
        "(salary location, date format, company name placement, etc.)"
    )
    page_structure: str = Field(
        description="2-3 sentences describing how the page organizes job data "
        "(heading hierarchy, section layout, where key fields appear)"
    )


def _generate_profile_hints(profile, scrape, user=None):
    """Use a cheap LLM call to generate extraction_hints and page_structure for a ScrapeProfile."""
    from job_hunting.models.ai_usage import AiUsage
    from job_hunting.lib.pricing import estimate_cost

    content = scrape.job_content or ""
    if not content:
        return

    model_name = os.environ.get("HINT_GENERATOR_MODEL", "openai:gpt-4o-mini")
    try:
        agent = Agent(model_name, output_type=ProfileHints)
    except Exception:
        logger.debug("Could not create hint agent with model %s", model_name)
        return

    prompt = f"""Analyze this scraped job posting content from {profile.hostname} and describe extraction patterns and page structure. Be concise — 2-3 sentences each. This will help future extractions from this domain.

Content (first 2000 chars):
{content[:2000]}"""

    try:
        result = agent.run_sync(prompt)
        hints = result.output

        profile.extraction_hints = hints.extraction_hints[:1000]
        profile.page_structure = hints.page_structure[:1000]
        profile.save()

        # Record AI usage
        usage = result.usage()
        request_tokens = usage.request_tokens or 0
        response_tokens = usage.response_tokens or 0
        cost_user = user or _get_genesis_user()
        if cost_user:
            AiUsage.objects.create(
                user=cost_user,
                agent_name="scrape_profile_hints",
                model_name=model_name,
                trigger="scrape_profile",
                request_tokens=request_tokens,
                response_tokens=response_tokens,
                total_tokens=usage.total_tokens or 0,
                request_count=1,
                estimated_cost_usd=estimate_cost(model_name, request_tokens, response_tokens),
            )

        logger.info("Generated hints for %s (%d tokens)", profile.hostname, usage.total_tokens or 0)
    except Exception:
        logger.debug("Hint generation LLM call failed for %s", profile.hostname, exc_info=True)
