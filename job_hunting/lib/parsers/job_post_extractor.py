import logging
import os
import re

from django_q.tasks import async_task
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

from pydantic import BaseModel, Field, field_validator
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.ollama import OllamaProvider

from urllib.parse import urlparse

from django.db.models import Q

from job_hunting.lib.job_post_merge import merge_empty_fields_from_attrs
from job_hunting.lib.scrapers.html_cleaner import clean_html_to_markdown
from job_hunting.lib.services.application_flow import STUB_MIN_WORDS
from job_hunting.lib.services.prompt_utils import write_prompt_to_file
from job_hunting.models import (
    Company,
    CompanyAlias,
    JobPost,
    JobPostDiscovery,
    JobPostOverwriteDecision,
    Scrape,
)
from job_hunting.models.job_post import audience_for_user
from job_hunting.models.job_post_dedupe import (
    canonicalize_link,
    prefer_extension_direct_link,
    source_trust,
)

logger = logging.getLogger(__name__)

# Fields the persist-update branches must never overwrite on an existing
# JobPost. `source` is JobPost provenance (set on creation only) — a
# later scrape must not flip an email-originated post to "scrape".
# `created_by` is the original owner and must never change.
_NO_OVERWRITE_FIELDS = {"created_by", "source"}

# Synthetic "[CLOSED — applications no longer accepted]" prefix the
# Tier1 extractor occasionally prepends when a profile extraction_hints
# blob instructs it to. Stripped before persistence — posting_status is
# the authoritative closed signal, not banner text in the description.
_CLOSED_BANNER_PREFIX = re.compile(
    r"^\s*(?:\*\*)?\s*\[\s*closed\b[^\]]*\]\s*(?:\*\*)?\s*\n*",
    flags=re.IGNORECASE,
)


def _to_jsonable(v):
    """Best-effort coercion to JSONField-safe types. Decimals and
    datetimes are stringified — the audit row only needs to be
    human-readable, not round-trippable."""
    if v is None or isinstance(v, (str, int, bool, float)):
        return v
    return str(v)


def _jsonify_diff(diff: dict) -> dict:
    return {
        field: {"before": _to_jsonable(c["before"]), "after": _to_jsonable(c["after"])}
        for field, c in diff.items()
    }


def _strip_closed_banner_prefix(text: str) -> str:
    """Remove a leading [CLOSED ...] banner the LLM may have synthesized.

    Substring-only at the START of the text (LLMs render banners as
    prefixes, not mid-sentence). Idempotent.
    """
    return _CLOSED_BANNER_PREFIX.sub("", text)


# Per-unit pay tokens that signal an hourly / daily / weekly / monthly
# figure in the source content. When any of these match scrape.job_content
# AND the LLM also returned salary_min / salary_max, the salary fields are
# almost certainly hallucinated annualizations (e.g. $60/hr → "salary_min:
# 124800"). Coerce both fields to None and keep the per-unit token in the
# description so the user can still read what the page actually said.
#
# Range form is matched explicitly (`$60-65/hr`, `$60 - $65 per hour`)
# because the leading `$\d` anchor would otherwise miss the second figure
# in a hyphenated range when the unit token appears only after the second
# number. Case-insensitive.
_PER_UNIT_PAY_TOKEN = re.compile(
    r"\$\s?\d[\d,.\s\-–$]*?"
    r"(?:/\s?hr|/\s?hour|per\s+hour|hourly"
    r"|/\s?day|per\s+day|daily"
    r"|/\s?wk|/\s?week|per\s+week|weekly"
    r"|/\s?mo|/\s?month|per\s+month|monthly)",
    flags=re.IGNORECASE,
)

# LinkedIn renders an "Estimated pay" chip on roles that did NOT disclose
# salary on the page; the LLM regularly mistakes that chip's number for an
# advertised band. The phrase alone is the signal — there's no real number
# attached.
_ESTIMATED_PAY_TOKEN = re.compile(
    r"estimated\s+pay",
    flags=re.IGNORECASE,
)

# extraction_date drift threshold. The LLM is asked for today's date but
# sometimes returns a hardcoded value from its training data (commonly
# 2023-10-06 / 2024-01 / a year-ago shadow), or echoes a posted_date
# field. If the returned extraction_date is more than this many days
# behind now() it's hallucinated — coerce to None rather than guessing a
# replacement. The model layer treats None as "unknown".
_EXTRACTION_DATE_DRIFT_DAYS = 30


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
    # Substring evidence the LLM saw a closed-posting banner. None when no
    # banner was present. The post-extraction validator confirms this string
    # appears in `job_content` before honoring it; otherwise we treat it as
    # hallucination and refuse to mark the post closed via this channel.
    # Background: jp 1550 (linkedin Lululemon, 2026-05-01) showed Tier1Mini
    # fabricating "[CLOSED — applications no longer accepted]" prefixes on
    # active postings driven by a profile extraction_hints instruction.
    closed_evidence: Optional[str] = Field(
        None,
        max_length=300,
        description=(
            "VERBATIM quote (5+ words) from the source page that proves the "
            "posting is closed (e.g. 'we are no longer accepting applications "
            "for this role'). MUST appear character-for-character in the "
            "source. Leave None if there is no such phrase."
        ),
    )

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


_DEFAULT_PARSER_MODEL = "openai:gpt-4o"


class JobPostExtractor:
    def __init__(self, client=None):
        self.client = client
        self.agent = None
        # None = tier 0 not attempted; True = tier 0 produced data;
        # False = tier 0 selectors present but didn't match required fields.
        self.last_tier0_hit: Optional[bool] = None
        # None = prefill not attempted; True = prefill produced data;
        # False = prefill dict present but didn't satisfy the title +
        # company_name gate.
        self.last_prefill_hit: Optional[bool] = None
        # Outcome of the most recent parse, used to shape the completion status
        # note so the frontend can branch flash messaging:
        #   "created"       — new JobPost inserted
        #   "updated_stub"  — existing thin-description post upgraded in place
        #   "duplicate"     — existing full-description post hit; scrape linked
        #                     but no fields overwritten
        self.last_outcome: Optional[str] = None

    def parse(self, scrape: Scrape, user=None, force: bool = False) -> bool:
        """Run extraction; return True if a valid JobPost was produced.

        When force=True, re-parse and merge fresh fields onto an already-
        linked JobPost (overwrite non-None extracted values, preserve
        company). Use for explicit re-scrape / re-parse / re-extract flows.
        """
        # Try the extension prefill first — the browser already ran the
        # per-host job_data selectors against the live DOM, so when title
        # + company_name landed we can build ParsedJobData without paying
        # an LLM round-trip.
        validated_data, prefill_attempted = self._try_prefill_extraction(scrape)
        if prefill_attempted:
            self.last_prefill_hit = validated_data is not None
        else:
            self.last_prefill_hit = None
        if validated_data:
            logger.info("Extension prefill extraction succeeded for scrape %s", scrape.id)
            self.last_tier0_hit = None
            return self.process_evaluation(scrape, validated_data, user=user, force=force)

        # Try Tier 0 (deterministic CSS extraction) next — $0 cost
        validated_data, tier0_attempted = self._try_tier0_extraction(scrape)
        if tier0_attempted:
            self.last_tier0_hit = validated_data is not None
        else:
            self.last_tier0_hit = None
        if validated_data:
            logger.info("Tier 0 extraction succeeded for scrape %s", scrape.id)
        else:
            validated_data = self.analyze_with_ai(scrape)
        return self.process_evaluation(scrape, validated_data, user=user, force=force)

    def _resolve_model_name(self) -> str:
        """Role-specific env var -> fallback env var -> default."""
        return (
            os.environ.get("JOB_PARSER_MODEL")
            or os.environ.get("CADDY_DEFAULT_MODEL")
            or _DEFAULT_PARSER_MODEL
        )

    def _build_agent_for_model(self, model_name: str) -> "Agent":
        """Construct a pydantic-ai Agent bound to the given model spec.

        Requires pydantic-ai `provider:model` notation (`openai:…`,
        `anthropic:…`, `ollama:…`) — dispatches to the matching SDK
        model class so the right provider's HTTP client is used. Bare
        names (no prefix) raise ValueError — env vars must spell out
        the provider so the wrong-client misroute that produced the
        Tier2 `model_not_found` incident (scrape #237 / jp 1550,
        2026-04-30) cannot recur silently. Used directly by the
        scrape-graph's Tier1/2/3 nodes when they need to escalate
        without reusing the cached default agent.
        """
        if ":" not in model_name:
            raise ValueError(
                f"Model spec {model_name!r} must use 'provider:model' form "
                "(e.g. 'openai:gpt-4o', 'anthropic:claude-haiku-4-5'). "
                "Set JOB_PARSER_MODEL or CADDY_DEFAULT_MODEL accordingly."
            )
        provider, bare_name = model_name.split(":", 1)
        if provider == "ollama":
            model = OpenAIChatModel(
                model_name=bare_name,
                provider=OllamaProvider(
                    base_url=os.environ.get("OLLAMA_API_BASE", "http://localhost:11434/v1")
                ),
            )
        elif provider == "anthropic":
            # Lazy import — keeps the anthropic SDK optional for installs
            # that only configure OpenAI/Ollama.
            from pydantic_ai.models.anthropic import AnthropicModel

            model = AnthropicModel(bare_name)
        elif provider == "openai":
            model = OpenAIResponsesModel(bare_name)
        else:
            raise ValueError(
                f"Unknown provider {provider!r} in model spec {model_name!r}. "
                "Supported: 'openai', 'anthropic', 'ollama'."
            )
        return Agent(model, output_type=ParsedJobData)

    def get_agent(self):
        """Return (and cache) the default agent resolved from env."""
        if self.agent:
            return self.agent
        self.agent = self._build_agent_for_model(self._resolve_model_name())
        return self.agent

    _PLACEHOLDER_NAMES = {"n/a", "na", "unknown", "none", "tbd", "not specified", ""}

    def _is_placeholder(self, value: str) -> bool:
        return (value or "").strip().lower() in self._PLACEHOLDER_NAMES

    def _coerce_implausible_fields(self, validated_data: "ParsedJobData", scrape: Scrape) -> None:
        """Drop fields the LLM almost certainly hallucinated.

        Called from the top of ``process_evaluation`` (after the
        placeholder rejection guard, before company resolution) so the
        coercion sits on every extraction pathway — prefill, Tier 0,
        Tier1+ analyze_with_ai, and direct ``process_evaluation`` calls.
        Two gates today:

        1. **Salary unit mismatch.** Per-unit pay tokens (``$60/hr``,
           ``$60 per hour``, LinkedIn "Estimated pay" chips, etc.) in
           ``scrape.job_content`` paired with non-None ``salary_min`` /
           ``salary_max`` mean the LLM annualized a non-annual figure
           (the canonical jp 1850-era incident: ``$60-65/hr`` → "salary
           band $124,800 - $135,200"). Drop both salary fields. The
           per-unit string survives in ``description`` for the user.
        2. **Extraction date drift.** ``extraction_date`` more than
           ``_EXTRACTION_DATE_DRIFT_DAYS`` behind ``datetime.now()`` is
           hallucinated (training-data echo, or LLM grabbed
           ``posted_date``). Coerce to None — don't guess today's date.

        Mutates ``validated_data`` in place. Mirrors the shape of the
        ``closed_evidence`` two-gate validator: when the source
        contradicts the LLM output, drop the value rather than persisting
        it.
        """
        raw_source = scrape.job_content or ""

        if validated_data.salary_min is not None or validated_data.salary_max is not None:
            per_unit_match = _PER_UNIT_PAY_TOKEN.search(raw_source)
            estimated_match = _ESTIMATED_PAY_TOKEN.search(raw_source)
            if per_unit_match or estimated_match:
                matched = (per_unit_match or estimated_match).group(0)
                logger.info(
                    "JobPostExtractor: coercing salary_min/salary_max to None "
                    "for scrape=%s — per-unit pay token in source "
                    "(matched=%r, original_min=%s, original_max=%s)",
                    scrape.id,
                    matched[:80],
                    validated_data.salary_min,
                    validated_data.salary_max,
                )
                validated_data.salary_min = None
                validated_data.salary_max = None

        if validated_data.extraction_date is not None:
            try:
                drift = datetime.now() - validated_data.extraction_date
            except TypeError:
                # Mixed tz-aware/naive — treat as drifted, the LLM has no
                # business returning a tz-aware value when our pydantic
                # model is naive.
                drift = timedelta(days=_EXTRACTION_DATE_DRIFT_DAYS + 1)
            if drift > timedelta(days=_EXTRACTION_DATE_DRIFT_DAYS):
                logger.info(
                    "JobPostExtractor: coercing extraction_date to None for "
                    "scrape=%s — value %s is %s days behind now()",
                    scrape.id,
                    validated_data.extraction_date,
                    drift.days,
                )
                validated_data.extraction_date = None

    def _resolve_company(self, scrape: Scrape, validated_data) -> Company:
        """Find or mint the Company for ``validated_data.company_name``.

        Phase A of the dedupe redesign. Order of operations:

        1. ``Company.find_by_alias(name)`` — exact match on
           ``slug(strip_corp_suffix(name))`` against
           ``CompanyAlias.name_slug``. Hit → attach to that Company.
        2. Literal-name ``get_or_create`` fallback — protects the
           pre-alias rollout window where some Companies have not yet
           been backfilled into the alias table.
        3. On a fresh mint, write a self-alias row
           (``source="extraction"``) so the next capture of the same
           name finds it via step 1, AND stash the top-3 trigram-
           similar candidates on the scrape for staff review.

        Fuzzy similarity is presentation-only — it never auto-attaches.
        The fuzzy candidates feed the "Suggested companies" callout
        the frontend renders on Scrape show.
        """
        from job_hunting.lib.slug import slug, strip_corp_suffix

        name = validated_data.company_name
        display_name = validated_data.company_display_name

        # Step 1: exact alias match.
        existing = Company.find_by_alias(name)
        if existing is not None:
            return existing

        # Step 2: literal-name fallback. During the rollout window not
        # every Company has been alias-backfilled yet; we still want
        # to attach to existing rows by literal name rather than mint
        # a duplicate. MultipleObjectsReturned defensively guarded —
        # ``name`` is unique on the model but historic data can race.
        try:
            company, created = Company.objects.get_or_create(
                name=name,
                defaults={"display_name": display_name},
            )
        except Company.MultipleObjectsReturned:
            company = Company.objects.filter(name=name).first()
            created = False

        if not created:
            return company

        # Step 3a: write the self-alias for the just-minted Company so
        # future captures of the same name hit step 1 instead of
        # racing here again. Guard against the unlikely case of two
        # mints colliding on the same slug in the same transaction —
        # the unique constraint on name_slug makes that a no-op.
        candidate_slug = slug(strip_corp_suffix(name))
        if candidate_slug:
            CompanyAlias.objects.get_or_create(
                name_slug=candidate_slug,
                defaults={
                    "company": company,
                    "name": name,
                    "source": CompanyAlias.SOURCE_EXTRACTION,
                },
            )

        # Step 3b: stash top-3 trigram-similar alias rows for staff
        # review. No minimum score threshold — always stash exactly
        # three (or fewer if the table is small). Surfaced by the
        # frontend as a "Suggested companies" callout on Scrape show;
        # the staff curator hits "Merge into…" if a suggestion is in
        # fact the same entity.
        try:
            suggestions = self._compute_company_suggestions(name, company)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception(
                "Failed to compute trigram suggestions for Company %s (%r): %s",
                company.id,
                name,
                exc,
            )
            suggestions = []
        if suggestions:
            scrape.company_suggestions = suggestions
            scrape.save(update_fields=["company_suggestions"])

        return company

    @staticmethod
    def _compute_company_suggestions(name: str, just_created: Company) -> list:
        """Return top-3 trigram-similar Companies as JSON-ready dicts.

        Excludes ``just_created`` from the result so the suggestion
        list is always "other Companies that look similar" — the
        fresh row would otherwise dominate its own suggestion set
        because the self-alias name slug is an exact match.
        """
        from django.contrib.postgres.search import TrigramSimilarity
        from job_hunting.lib.slug import slug, strip_corp_suffix

        candidate_slug = slug(strip_corp_suffix(name))
        if not candidate_slug:
            return []
        qs = (
            CompanyAlias.objects.exclude(company_id=just_created.id)
            .annotate(similarity=TrigramSimilarity("name_slug", candidate_slug))
            .order_by("-similarity")
            .values("company_id", "name", "similarity")[:3]
        )
        return [
            {
                "company_id": row["company_id"],
                "name": row["name"],
                "similarity": float(row["similarity"]) if row["similarity"] is not None else 0.0,
            }
            for row in qs
        ]

    def process_evaluation(self, scrape: Scrape, validated_data: ParsedJobData, user=None, force: bool = False) -> bool:
        self.last_outcome = "created"
        if self._is_placeholder(validated_data.title):
            logger.warning("Scrape %s: extracted title is a placeholder (%r), skipping", scrape.id, validated_data.title)
            # Diagnostic surface for the operator — the extension popup
            # and scrapes.show read this field so a placeholder rejection
            # is visible without reading container logs.
            scrape.failure_reason = (
                f"Extraction returned placeholder title: {validated_data.title!r}"
            )[:2000]
            scrape.status = "failed"
            scrape.save()
            return False
        if self._is_placeholder(validated_data.company_name):
            logger.warning("Scrape %s: extracted company is a placeholder (%r), skipping", scrape.id, validated_data.company_name)
            scrape.failure_reason = (
                f"Extraction returned placeholder company: {validated_data.company_name!r}"
            )[:2000]
            scrape.status = "failed"
            scrape.save()
            return False

        # Field-tolerant coercion — drop values the LLM almost certainly
        # hallucinated (per-unit pay annualized to a salary band, ancient
        # extraction_date echo). Placed here so it sits on every pathway
        # into process_evaluation: prefill, Tier 0, Tier1+ analyze_with_ai,
        # and direct callers. See _coerce_implausible_fields docstring.
        self._coerce_implausible_fields(validated_data, scrape)

        # Find or create company — Company is a shared resource (no user scoping).
        #
        # Phase A of the dedupe redesign: gate on
        # ``Company.find_by_alias`` first (exact ``CompanyAlias.name_slug``
        # match), then fall back to literal ``name`` get_or_create, and on
        # mint of a fresh row write the self-alias plus stash top-3
        # trigram-similar candidates on the scrape for staff review. The
        # fuzzy candidates NEVER auto-attach — Doug's option (b) gate: only
        # exact ``name_slug`` match auto-resolves. See plan
        # ``go-over-this-plan-staged-sutherland.md`` Phase A and api
        # notes.org ``Architecture/Dedupe pipeline contract``.
        company = self._resolve_company(scrape, validated_data)

        # Effective description: prefer LLM output; fall back to the
        # user's raw paste when the LLM omitted it AND the scrape is
        # paste-sourced (the paste IS the description). Browser-fetched
        # scrapes can't fall back this way — their job_content is HTML/
        # page text with nav/footer noise. The fallback is only consumed
        # by the fresh-create path below; the force/link-hit branches
        # still gate on validated_data.description so an LLM-omitted
        # description never overwrites an existing populated one
        # (test_reextract_skips_none_fields).
        effective_description = validated_data.description
        if (
            not effective_description
            and getattr(scrape, "source", None) in ("paste", "extension")
        ):
            fallback = (scrape.job_content or "").strip()
            if fallback:
                effective_description = fallback

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
        # Read posting_status off the source text — NOT off the LLM
        # description, which can be polluted by hallucinated banners
        # (jp 1550 incident, 2026-05-01: linkedin extraction_hints
        # told Tier1Mini to lead the description with
        # "[CLOSED — applications no longer accepted]" on an active
        # posting). Two-channel detection:
        #   1. closed_evidence — the LLM quotes the source verbatim;
        #      we substring-validate against scrape.job_content. If the
        #      quote is real → posting is closed.
        #   2. detect_posting_status on scrape.job_content (the raw
        #      page text) — phrase-match against the curated list.
        # We do NOT trust phrase detection on the LLM-rendered
        # description anymore: the LLM is a regex-defeating attacker
        # in this threat model.
        from job_hunting.lib.text_signals import detect_posting_status

        raw_source = (scrape.job_content or "").strip()
        detected_status = None
        # Channel 1 (highest precedence): result from the scrape-graph
        # DetectClosedState node, written into Scrape.detected_posting_status
        # on PersistScrape. The graph already validated CSS / phrase / LLM
        # quote against the live DOM + captured text, so this is the
        # most-trusted signal. Bypasses the curated regex scan and the
        # LLM-emitted closed_evidence path below. Only the agents-side
        # scrape sub-graph populates this — paste/email/chat ingest
        # routes that skip the sub-graph leave it None and fall through
        # to the legacy channels.
        graph_status = (getattr(scrape, "detected_posting_status", None) or "").strip() or None
        if graph_status == "closed":
            detected_status = "closed"
            graph_evidence = (getattr(scrape, "detected_closed_evidence", None) or "").strip()
            logger.info(
                "JobPostExtractor: graph DetectClosedState verdict for "
                "scrape=%s (evidence=%r)", scrape.id, graph_evidence[:80],
            )
        if detected_status is None and raw_source:
            detected_status = detect_posting_status(raw_source)
        if (
            detected_status is None
            and getattr(validated_data, "closed_evidence", None)
        ):
            evidence = (validated_data.closed_evidence or "").strip()
            # Two-gate validation: the quote must (a) appear verbatim in
            # job_content (anti-hallucination) AND (b) itself match a
            # curated closed-state phrase from text_signals._CLOSED_PHRASES
            # (semantic gate). Without (b), the LLM could quote any short
            # snippet of UI chrome — "Promoted by hirer", "Save", etc. —
            # and silently flip the post to closed. jp 1532 incident
            # (2026-05-01): a degraded LinkedIn capture (756 chars,
            # mostly nav chrome, no closed banner) flipped to closed
            # because the substring guard alone accepted whatever the
            # LLM emitted as a "quote". The curated phrase list is the
            # same one that path #1 uses, so the two channels are now
            # symmetric: a quote that wouldn't trip path #1 mustn't
            # trip path #2 either.
            if evidence and evidence in raw_source and detect_posting_status(evidence) == "closed":
                detected_status = "closed"
                logger.info(
                    "JobPostExtractor: closed_evidence substantiated for "
                    "scrape=%s (quote=%r)", scrape.id, evidence[:80],
                )
            elif evidence and evidence not in raw_source:
                logger.warning(
                    "JobPostExtractor: discarding unsubstantiated "
                    "closed_evidence for scrape=%s — quote not present in "
                    "job_content (quote=%r)",
                    scrape.id, evidence[:80],
                )
            elif evidence:
                logger.warning(
                    "JobPostExtractor: discarding closed_evidence for "
                    "scrape=%s — quote present in job_content but does "
                    "not match any curated closed-state phrase "
                    "(quote=%r)",
                    scrape.id, evidence[:80],
                )
        if detected_status is not None:
            job_defaults["posting_status"] = detected_status

        # Belt-and-suspenders: strip any "[CLOSED ...]" / "**[CLOSED ...]**"
        # prefix the LLM may have prepended to the description. Even if
        # the source text genuinely is closed, that prefix is a synthetic
        # rendering decision — the column-level posting_status above is
        # the authoritative signal. Without this strip, the regex in
        # text_signals.py keeps re-firing on every re-parse and any
        # downstream consumer reading description sees a banner that
        # didn't come from the page.
        if "description" in job_defaults and job_defaults["description"]:
            job_defaults["description"] = _strip_closed_banner_prefix(
                job_defaults["description"]
            )
        # `source` is JobPost provenance — set on creation only. Re-scrapes
        # of an existing post must NOT clobber the original origin (e.g. an
        # email-originated stub being upgraded by a later scrape stays
        # source="email"). Per-scrape provenance lives on Scrape.source
        # and per-user discovery on JobPostDiscovery.source. Held in a
        # local rather than `job_defaults` so the update branches don't
        # smear it.
        create_only_source = getattr(scrape, "source", None) or "manual"

        # Use the scrape URL as the canonical link — it's the known-good source.
        # The LLM-extracted link may be an apply URL, redirect, or null.
        link = scrape.url or validated_data.link

        # When force=True and the scrape is already linked to a JobPost,
        # merge fresh fields onto THAT specific post — overwrite non-None
        # extracted values, preserve the existing company. Used by explicit
        # re-parse / re-extract flows where the user asked for an update.
        job = None
        if force and scrape.job_post_id:
            job = JobPost.objects.filter(pk=scrape.job_post_id).first()
            if job is not None:
                update_fields = []
                # Title is validated as non-empty above but lives outside
                # job_defaults (it's a get_or_create lookup key on the cold
                # path); pull it in explicitly for the force-merge path.
                if validated_data.title and job.title != validated_data.title:
                    job.title = validated_data.title
                    update_fields.append("title")
                for field, value in job_defaults.items():
                    if value is None or field in _NO_OVERWRITE_FIELDS:
                        continue
                    if getattr(job, field) != value:
                        setattr(job, field, value)
                        update_fields.append(field)
                if update_fields:
                    job.save(update_fields=update_fields)
                    self.last_outcome = "force_updated"
                else:
                    # Surfaces in the scrape-status note as
                    # "force_noop: nothing to update" so the frontend can
                    # tell the user their re-parse changed nothing —
                    # otherwise a "wasted parse" looks identical to a
                    # successful one (job-posts/1490 inbox bug).
                    self.last_outcome = "force_noop"
                if not job.complete:
                    job.complete = True
                    job.save(update_fields=["complete"])

        # Prefer link-based lookup since link is unique. Branch on
        # complete-vs-not: an incomplete hit is safe to overwrite
        # ("upgrade the stub"); a complete hit is a duplicate and
        # should keep its existing data intact.
        #
        # Match on raw link OR canonical_link, mirroring the from-text
        # endpoint's dedup query (scrapes.py). Without the canonical
        # leg, a /comm/jobs/view/ email-stub is invisible to a
        # /jobs/view/ extension push (LinkedIn redirects the email
        # form to the canonical), and the upgrade-the-stub branch
        # is bypassed — extractor falls through to get_or_create on
        # (title, company), which forks a new JobPost when the
        # extractor finds the company but the existing stub had
        # company=NULL. jp 1918 / jp 1922 incident (2026-05-08).
        link_hit = None
        if job is None and link:
            canonical = canonicalize_link(link)
            # Order complete=True first when multiple JPs share the
            # canonical_link (link is unique, but canonical_link isn't):
            # the complete row is the post the user cares about, and an
            # unordered .first() picking the stub would route through
            # the "updated_stub" path and silently smear into the wrong
            # JP. Mirrors the same defensive ordering in from-text.
            link_hit = (
                JobPost.objects
                .filter(Q(link=link) | Q(canonical_link=canonical))
                .order_by("-complete", "id")
                .first()
            )
        if link_hit is not None:
            job = link_hit
            # Trust-rank overwrite first: extension > paste > scrape >
            # ... > email. When a higher-trust source lands on a lower-
            # trust existing post, blow away the existing fields. This
            # is the self-heal path for cc_auto-hallucinated posts (the
            # jp 1724 SNBL case) — the extension push from the real
            # page replaces the email-pipeline's wrong title/company in
            # place, leaving an audit row in JobPostOverwriteDecision.
            if self._trust_aware_overwrite(
                job=job, scrape=scrape, validated_data=validated_data,
                job_defaults=job_defaults, company=company,
                link=link, user=user,
            ):
                pass  # _trust_aware_overwrite set last_outcome and saved
            elif not link_hit.complete:
                self.last_outcome = "updated_stub"
                update_fields = []
                if validated_data.title and job.title != validated_data.title:
                    job.title = validated_data.title
                    update_fields.append("title")
                if not job.company_id and company is not None:
                    job.company = company
                    update_fields.append("company_id")
                for field, value in job_defaults.items():
                    if value is None or field in _NO_OVERWRITE_FIELDS:
                        continue
                    if getattr(job, field) != value:
                        setattr(job, field, value)
                        update_fields.append(field)
                # The graph's ReviewCompleteness gate is the authoritative
                # signal; flip eagerly here and let the gate flip back to
                # False if the scraped output still doesn't read like a
                # job description.
                job.complete = True
                update_fields.append("complete")
                job.save(update_fields=update_fields)
            else:
                self.last_outcome = "duplicate"
                # Even on a full-description duplicate, fill any NULL/empty
                # fields the existing post is missing — same merge policy
                # the create() endpoint uses on its dedupe paths. Without
                # this the hold-poller silently drops the scrape's
                # company_id / posting_status / source onto the floor.
                merge_attrs = {
                    k: v for k, v in job_defaults.items()
                    if k not in _NO_OVERWRITE_FIELDS
                }
                if company is not None and not job.company_id:
                    merge_attrs["company_id"] = company.id
                merge_empty_fields_from_attrs(job, merge_attrs)
                # Layer 2 arbiter: when both descriptions are non-thin, ask
                # the LLM which is better and apply its decision.
                if effective_description:
                    self._maybe_apply_arbiter(
                        job=job, scrape=scrape,
                        new_description=effective_description,
                        new_source=getattr(scrape, "source", None) or "",
                        new_link=link or "",
                    )
        elif job is None:
            create_defaults = {**job_defaults, "link": link, "source": create_only_source}
            if "description" not in create_defaults and effective_description:
                create_defaults["description"] = effective_description
            # BACK-91: ingestion is private by default. Promote a freshly
            # ingested post to public only when its owner opted into
            # publishing (Profile.federate_posts). Set in `defaults` so it
            # applies only on the created=True branch — an existing-row
            # match (created=False) keeps its own audience untouched.
            create_defaults["audience"] = audience_for_user(user)
            job, created = JobPost.objects.get_or_create(
                title=validated_data.title,
                company=company,
                defaults=create_defaults,
            )
            # When get_or_create matches an existing post (created=False),
            # `defaults` are silently dropped. Without this block the
            # cold-path (title, company) match is asymmetric with the
            # link-hit branch above: a thin existing post would upgrade
            # on a link match but never on a fingerprint match.
            # jp 1603 incident (2026-05-02): an extension scrape from
            # welcometothejungle.com deduped to an email-sourced LinkedIn
            # JP via title+company, and the rich description was lost.
            if not created:
                # Trust-rank overwrite first — symmetric with the
                # link-hit branch above. A higher-trust extension push
                # that title+company-matches an email post still wins.
                if self._trust_aware_overwrite(
                    job=job, scrape=scrape, validated_data=validated_data,
                    job_defaults=job_defaults, company=company,
                    link=link, user=user,
                ):
                    pass
                elif (
                    not job.complete
                    and effective_description
                    and len(effective_description.split()) >= STUB_MIN_WORDS
                ):
                    self.last_outcome = "updated_stub_via_fingerprint"
                    update_fields = []
                    for field, value in job_defaults.items():
                        if value is None or field in _NO_OVERWRITE_FIELDS:
                            continue
                        if getattr(job, field) != value:
                            setattr(job, field, value)
                            update_fields.append(field)
                    job.complete = True
                    update_fields.append("complete")
                    job.save(update_fields=update_fields)
                else:
                    self.last_outcome = "duplicate_via_fingerprint"
                    merge_attrs = {
                        k: v for k, v in job_defaults.items()
                        if k not in _NO_OVERWRITE_FIELDS
                    }
                    if company is not None and not job.company_id:
                        merge_attrs["company_id"] = company.id
                    merge_empty_fields_from_attrs(job, merge_attrs)
                    # Layer 2 arbiter: same as the link-hit duplicate path.
                    if effective_description:
                        self._maybe_apply_arbiter(
                            job=job, scrape=scrape,
                            new_description=effective_description,
                            new_source=getattr(scrape, "source", None) or "",
                            new_link=link or "",
                        )
        elif not force or not scrape.job_post_id:
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

        # Posting-status update is sticky-once-closed: any persist
        # branch above that runs into an existing post will skip
        # job_defaults["posting_status"] (the non-force / non-stub
        # branch only fills NULL fields). For the close signal we want
        # the opposite — re-scraping a previously-open post that's now
        # showing closed phrases SHOULD flip it. The detector never
        # returns "open" (only "closed" or None), so this overwrite
        # only ever moves towards closed and never silently flips
        # closed back to None on a re-scrape that didn't fire any
        # phrase.
        if detected_status == "closed" and job.posting_status != "closed":
            job.posting_status = "closed"
            job.save(update_fields=["posting_status"])

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

        # Record JobPostDiscovery for the scrape's owner so the post is
        # visible to them via the canonical discovery signal — same shape
        # the create() endpoint records on POST. Without this the only
        # signal connecting the user to the post is `scrapes__created_by`,
        # which we want to retire as discovery becomes the canonical edge.
        if scrape.created_by_id:
            JobPostDiscovery.objects.get_or_create(
                job_post=job,
                user_id=scrape.created_by_id,
                defaults={"source": getattr(scrape, "source", None) or "scrape"},
            )

        # Roll the dedupe window forward. Every persist branch above
        # either created a fresh row (where last_seen_at defaults to
        # now()) or resolved the scrape to an existing row. For the
        # existing-row case this is the bump that keeps the row in
        # ``find_duplicate``'s fingerprint window past 30 days — without
        # it, a long-tail role rescraped from a different host (JP 1329
        # / 42 days old at the rolling-window enhancement) would fall
        # out of the window and re-fork. Cheap idempotent UPDATE on the
        # create branch; load-bearing on the dedupe branch.
        from job_hunting.models.job_post_dedupe import bump_last_seen
        bump_last_seen(job)
        return True

    def _trust_aware_overwrite(
        self,
        *,
        job,
        scrape,
        validated_data,
        job_defaults,
        company,
        link,
        user,
    ) -> bool:
        """Higher-trust source wins on collision.

        When the incoming scrape's source outranks the existing post's
        source (per ``source_trust``), overwrite every overwritable
        field on the existing post with the parsed values, flip
        ``source`` to the new source, and write a
        ``JobPostOverwriteDecision`` audit row capturing the diff.

        Returns True iff an overwrite happened. Callers should treat
        True as the terminal field-write outcome and skip the
        thin/non-thin merge branches.
        """
        new_source = getattr(scrape, "source", None) or "manual"
        existing_source = job.source or "manual"
        if source_trust(new_source) <= source_trust(existing_source):
            return False

        diff: dict[str, dict] = {}

        # Title and company are special — they're get_or_create lookup
        # keys upstream and never appear in job_defaults.
        if validated_data.title and job.title != validated_data.title:
            diff["title"] = {"before": job.title, "after": validated_data.title}
            job.title = validated_data.title
        if company is not None and job.company_id != company.id:
            diff["company_id"] = {"before": job.company_id, "after": company.id}
            job.company = company

        # Link is canonical-derived — recompute canonical_link in save().
        #
        # Phase A — Extension direct-POST plan merge-bias rule. When the
        # incoming or existing scrape carries source_mode='extension-
        # direct', the extension-direct row's link wins regardless of
        # which side it sits on. The user-rendered URL is more
        # trustworthy than a server-side-fetched URL (the extension can
        # only fire on a tab the user actually navigated to; background
        # scrapes routinely land on tracker/SSO variants the user
        # never sees). The other fields below still apply the
        # empty-merge invariant via the job_defaults loop, so this only
        # changes the link decision.
        chosen_link = prefer_extension_direct_link(job, scrape, link)
        if chosen_link and job.link != chosen_link:
            diff["link"] = {"before": job.link, "after": chosen_link}
            job.link = chosen_link
            job.canonical_link = None

        for field, value in job_defaults.items():
            if value is None or field in _NO_OVERWRITE_FIELDS:
                continue
            current = getattr(job, field, None)
            if current != value:
                diff[field] = {"before": current, "after": value}
                setattr(job, field, value)

        # The whole point of this path: source flips on collision so the
        # next ranking comparison reads the new (higher) trust level.
        diff["source"] = {"before": existing_source, "after": new_source}
        job.source = new_source

        # Mirror the updated_stub branch's eager flip: a higher-trust
        # source overwriting an email/scrape stub fills the fields, so
        # the post is no longer "incomplete" by definition. ReviewCompleteness
        # remains authoritative and will flip back to False if the
        # parsed output still doesn't read like a job description.
        if not job.complete:
            diff["complete"] = {"before": False, "after": True}
            job.complete = True

        job.save()

        JobPostOverwriteDecision.objects.create(
            job_post=job,
            triggering_scrape=scrape,
            previous_source=existing_source,
            new_source=new_source,
            changed_fields=_jsonify_diff(diff),
            created_by=user,
        )
        self.last_outcome = "overwritten"
        return True

    def _maybe_apply_arbiter(
        self,
        *,
        job,
        scrape,
        new_description: str,
        new_source: str,
        new_link: str,
    ) -> None:
        """Run the DescriptionArbiter and apply the winning description.

        Only fires when the existing post is flagged complete — an
        incomplete post should land in the upgrade branch upstream
        rather than ask the arbiter which description is better. A
        keep_existing result is a no-op on the job row but always
        produces an audit record.
        """
        if not job.complete:
            return

        from job_hunting.lib.parsers.description_arbiter import maybe_arbitrate_and_persist

        winning = maybe_arbitrate_and_persist(
            job_post=job,
            scrape=scrape,
            new_description=new_description,
            new_source=new_source,
            new_link=new_link,
        )
        if winning is None:
            return
        current = (job.description or "").strip()
        if winning != current:
            job.description = winning
            job.save(update_fields=["description"])

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
        # Match by class name to avoid forcing the anthropic SDK import
        # at module load — _build_agent_for_model imports it lazily.
        cls_name = type(model).__name__
        if cls_name == "AnthropicModel":
            return f"anthropic:{getattr(model, 'model_name', model)}"
        return str(model)

    def _get_profile_hints(self, scrape: Scrape) -> str:
        """Look up ScrapeProfile for this scrape's hostname and return hint text.

        Extension-source bypass: when ``scrape.source == 'extension'``, the
        job_content came from the cc_sender extension's live-DOM capture
        — the user's browser actually rendered the page and the extension
        read the post-hydration text. Partial-render heuristics (the
        load-bearing reason ScrapeProfile.extraction_hints exists for
        linkedin.com) are therefore meaningless and actively harmful on
        this path: the LinkedIn linkedin.com hint dutifully written by
        the LLM emits "[DESCRIPTION NOT CAPTURED ...]" whenever it sees
        the top-card + footer sentinel pair, but those sentinels appear
        in EVERY healthy extension capture too. Bypassing the hint block
        on this path lets the LLM extract the real body verbatim.
        Belt-and-suspenders alongside the 0101 migration that tightens
        the linkedin.com hint itself.
        """
        if getattr(scrape, "source", None) == "extension":
            return ""
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

    # Field length floor for accepting the prefill bypass. h1 misfires on
    # error pages ("Error", "404") collapse short; a 3-char title + company
    # gate keeps those out without rejecting legitimate short titles like
    # "PM" or "QA".
    _PREFILL_MIN_FIELD_LEN = 3

    def _try_prefill_extraction(
        self, scrape: Scrape,
    ) -> tuple[Optional[ParsedJobData], bool]:
        """Attempt extraction from the extension-supplied structured prefill.

        The browser extension reads ScrapeProfile.css_selectors.job_data via
        the extension-selectors endpoint, runs each selector against the
        live DOM, and posts the resulting dict on /scrapes/from-text/. We
        persist it on Scrape.extension_prefill and bypass the LLM here
        when title + company_name are both populated.

        Returns a (data, attempted) tuple:
          - (ParsedJobData, True)  — title + company_name plausible.
          - (None, True)           — Prefill present but missing required
                                     fields / failed plausibility gate.
          - (None, False)          — No prefill on the scrape.
        """
        prefill = getattr(scrape, "extension_prefill", None)
        if not isinstance(prefill, dict) or not prefill:
            return None, False

        title = (prefill.get("title") or "").strip()
        company = (
            prefill.get("company_name") or prefill.get("company") or ""
        ).strip()
        if (
            len(title) < self._PREFILL_MIN_FIELD_LEN
            or len(company) < self._PREFILL_MIN_FIELD_LEN
        ):
            return None, True

        description = (prefill.get("description") or "").strip() or None
        location = (prefill.get("location") or "").strip() or None
        try:
            data = ParsedJobData(
                title=title,
                company_name=company,
                description=description,
                location=location,
                remote=None,
                salary_min=None,
                salary_max=None,
                link=scrape.url,
                extraction_date=datetime.now(),
            )
        except Exception:
            logger.debug(
                "Prefill validation failed scrape_id=%s prefill=%r",
                scrape.id, prefill, exc_info=True,
            )
            return None, True
        return data, True

    def _try_tier0_extraction(self, scrape: Scrape) -> tuple[Optional[ParsedJobData], bool]:
        """Attempt deterministic extraction using CSS selectors from ScrapeProfile.

        Returns a (data, attempted) tuple:
          - (ParsedJobData, True)  — Tier 0 matched title + company.
          - (None, True)           — Selectors were present but didn't match
                                     required fields (Tier 0 miss).
          - (None, False)          — Tier 0 wasn't tried (no html, no profile,
                                     no selectors, or profile opted out).
        """
        if not scrape.html:
            return None, False

        try:
            from bs4 import BeautifulSoup
            from job_hunting.models import ScrapeProfile

            hostname = urlparse(scrape.url or "").hostname or ""
            if hostname.startswith("www."):
                hostname = hostname[4:]
            if not hostname:
                return None, False

            profile = ScrapeProfile.objects.filter(
                hostname=hostname, enabled=True
            ).first()
            if not profile or not profile.css_selectors:
                return None, False
            if profile.preferred_tier not in ("auto", "0"):
                return None, False

            selectors = profile.css_selectors
            if not isinstance(selectors, dict):
                return None, False

            job_selectors = selectors.get("job_data", {})
            if not job_selectors:
                return None, False

            soup = BeautifulSoup(scrape.html, "html.parser")
            extracted = {}
            for field, selector in job_selectors.items():
                el = soup.select_one(selector)
                if el:
                    extracted[field] = el.get_text(strip=True)

            title = extracted.get("title", "")
            company = extracted.get("company_name", "") or extracted.get("company", "")
            if not title or not company:
                # Selectors ran but didn't produce required fields — this is a miss.
                return None, True

            logger.info(
                "Tier 0 extraction for %s: title=%s, company=%s",
                hostname, title[:50], company[:50],
            )

            return (
                ParsedJobData(
                    title=title,
                    company_name=company,
                    description=extracted.get("description"),
                    location=extracted.get("location"),
                    remote=None,
                    salary_min=None,
                    salary_max=None,
                    link=scrape.url,
                    extraction_date=datetime.now(),
                ),
                True,
            )
        except Exception:
            logger.debug("Tier 0 extraction failed", exc_info=True)
            return None, False

    def analyze_with_ai(
        self, scrape: Scrape, model_override: Optional[str] = None
    ) -> ParsedJobData:
        """Run LLM extraction against the scrape content.

        `model_override` lets the scrape-graph's Tier1/2/3 nodes target
        a specific model without mutating the cached default agent.
        Legacy callers (parse_scrape) pass no override and get the
        env-resolved default, preserving today's behavior.
        """
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

        if model_override:
            agent = self._build_agent_for_model(model_override)
        else:
            if self.agent is None:
                self.agent = self.get_agent()
            agent = self.agent

        result = agent.run_sync(prompt)
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


def parse_scrape(scrape_id: int, user_id: int = None, sync: bool = False, force: bool = False) -> None:
    """
    Single entry point for parsing a scrape into a JobPost + Company.

    Handles status transitions (extracting -> completed/failed), error logging,
    and user resolution. By default, bails out if scrape.job_post_id is
    already set (idempotency guard for the first-pass pipeline). Pass
    `force=True` to re-parse and merge fresh fields onto the linked JobPost
    — used by explicit Re-run scrape / manual Parse / Re-extract flows.

    Args:
        scrape_id: PK of the Scrape record.
        user_id: PK of the user to attribute created records to.
                 Falls back to scrape.created_by if None.
        sync: If True, run inline (use when already in a background thread).
              If False, spawn a daemon thread.
        force: If True, overwrite non-None fields on the already-linked
               JobPost (company preserved). Skips the early-bail check.
    """

    def _run():
        from job_hunting.models.scrape import Scrape as ScrapeModel
        from job_hunting.lib.scraper import _log_scrape_status
        from django.contrib.auth import get_user_model

        # Safety net: guarantee the scrape leaves `extracting` /
        # `updating_profile` no matter what blows up below — uncaught
        # exception, container restart racing the LLM call, OOM kill of
        # _update_scrape_profile, etc. The frontend polls scrape.status
        # to terminal; a stuck row hangs the user's UI forever.
        # Companion: management command sweep_stuck_extracting cleans up
        # the rare cases where finally itself can't run (SIGKILL).
        reached_terminal = False
        # Threaded through the outer except into the finally — the
        # failure_reason persisted on the Scrape row when the safety
        # net trips. Initialized here so both branches read the same
        # name regardless of whether the except fired.
        outer_failure_reason = None
        try:
            scrape = ScrapeModel.objects.filter(pk=scrape_id).first()
            if not scrape:
                logger.warning("parse_scrape: scrape_id=%s not found", scrape_id)
                reached_terminal = True
                return
            if scrape.job_post_id and not force:
                logger.info("parse_scrape: scrape_id=%s already has job_post, skipping", scrape_id)
                reached_terminal = True
                return
            if not (scrape.job_content or scrape.html):
                logger.warning("parse_scrape: scrape_id=%s has no content", scrape_id)
                reached_terminal = True
                return

            user = None
            if user_id:
                User = get_user_model()
                user = User.objects.filter(pk=user_id).first()
            if not user:
                user = scrape.created_by

            _log_scrape_status(scrape_id, "extracting")

            parser = JobPostExtractor()
            success = False
            # Capture the parser exception (if any) so the terminal
            # failed-status write can carry it through to
            # Scrape.failure_reason. Without this the operator-facing
            # surface stays generic ("Extraction failed") while the
            # actual exception only lives in the api container log.
            parse_exc = None
            try:
                success = bool(parser.parse(scrape, user=user, force=force))
            except Exception as exc:
                parse_exc = exc
                logger.exception("parse_scrape failed scrape_id=%s", scrape_id)

            _log_scrape_status(scrape_id, "updating_profile", note="Updating scrape profile")

            try:
                _update_scrape_profile(
                    scrape, user,
                    success=success,
                    tier0_hit=parser.last_tier0_hit,
                )
            except Exception:
                logger.debug("Failed to update scrape profile", exc_info=True)

            if success:
                outcome = getattr(parser, "last_outcome", None) or "created"
                scrape.refresh_from_db(fields=["job_post_id"])
                jp_id = scrape.job_post_id
                if outcome == "duplicate" and jp_id:
                    note = f"duplicate: existing JobPost #{jp_id}"
                elif outcome == "updated_stub" and jp_id:
                    note = f"updated_stub: upgraded JobPost #{jp_id}"
                elif outcome == "force_noop" and jp_id:
                    note = f"force_noop: re-parse of JobPost #{jp_id} found no new fields"
                elif outcome == "force_updated" and jp_id:
                    note = f"force_updated: refreshed JobPost #{jp_id}"
                else:
                    note = "Parsed successfully"
                # Final gate: does the persisted output actually look like a
                # job description? Only fires for outcomes that actually
                # touched the description; pure dedup hits skip the LLM.
                # On rejection: flip JobPost.complete=False AND mark this
                # scrape failed (the JP still exists so the URL lookup
                # works; the user / extension can re-scrape it).
                review_rejected = False
                review_reason = None
                if jp_id:
                    try:
                        from job_hunting.lib.parsers.completeness_reviewer import (
                            maybe_review_and_persist,
                        )
                        from job_hunting.models import JobPost
                        jp = JobPost.objects.filter(pk=jp_id).first()
                        if jp is not None:
                            decision = maybe_review_and_persist(jp, last_outcome=outcome)
                            if decision is not None and not decision.looks_like_job_description:
                                review_rejected = True
                                review_reason = decision.reasoning
                    except Exception:
                        logger.exception(
                            "CompletenessReviewer failed for JP %s; "
                            "leaving complete flag untouched",
                            jp_id,
                        )
                if review_rejected:
                    review_msg = review_reason or "review rejected output"
                    _log_scrape_status(
                        scrape_id, "failed",
                        note=f"incomplete_output: {review_msg}",
                        failure_reason=f"CompletenessReviewer rejected output: {review_msg}",
                    )
                    reached_terminal = True
                else:
                    _log_scrape_status(scrape_id, "completed", note=note)
                    reached_terminal = True
            else:
                # Two shapes for the failed branch:
                #   1. parser.parse raised — thread the exception repr
                #      into failure_reason so the operator sees it.
                #   2. parser.parse returned falsy (placeholder rejection
                #      in process_evaluation already wrote a richer
                #      reason directly on the Scrape row). DO NOT
                #      overwrite that with a generic string — pass
                #      failure_reason=None so _log_scrape_status leaves
                #      the existing column alone.
                if parse_exc is not None:
                    fr = f"parse_scrape exception: {parse_exc!r}"
                else:
                    fr = None
                try:
                    _log_scrape_status(
                        scrape_id, "failed",
                        note="Extraction failed",
                        failure_reason=fr,
                    )
                except Exception:
                    pass
                reached_terminal = True
        except Exception as outer_exc:
            logger.exception("parse_scrape: unhandled in _run scrape_id=%s", scrape_id)
            # Thread the unhandled exception through so the finally
            # branch below can surface it to the operator.
            outer_failure_reason = f"parse_scrape unhandled exception: {outer_exc!r}"
        finally:
            if not reached_terminal:
                fr = outer_failure_reason or (
                    "parse_scrape died before terminal — see api logs"
                )
                try:
                    _log_scrape_status(
                        scrape_id, "failed",
                        note="parse_scrape died before terminal — see api logs",
                        failure_reason=fr,
                    )
                except Exception:
                    logger.exception(
                        "parse_scrape: failed to flip stuck scrape_id=%s to failed",
                        scrape_id,
                    )

    if sync:
        _run()
    else:
        # Phase 5a of Plans/Job-queue integration. parse_scrape used to
        # fork a daemon thread when sync=False; now the work runs on the
        # qcluster worker. The task target re-enters parse_scrape with
        # sync=True so the body above runs inline inside the worker.
        async_task(
            "job_hunting.lib.tasks.parse_scrape_job",
            scrape_id,
            user_id=user_id,
            force=force,
        )


_TIER0_DEMOTE_MIN_MISSES = 5
_TIER0_DEMOTE_MISS_RATIO = 0.5


def _update_scrape_profile(scrape, user=None, success: bool = True, tier0_hit: Optional[bool] = None):
    """Create or update ScrapeProfile for this scrape's hostname.

    Args:
        success: True on a fully-extracted JobPost, False on any failure
                 (exception, placeholder title/company, etc.). Failures
                 pull success_rate down toward 0.0.
        tier0_hit: True = tier 0 matched; False = tier 0 selectors ran but
                   missed required fields; None = tier 0 not attempted.
                   A False bumps tier0_miss_count and may auto-demote
                   preferred_tier from 'auto' to '1' so future extractions
                   skip Tier 0 until selectors are refreshed.
    """
    from django.utils import timezone
    from job_hunting.models import ScrapeProfile

    hostname = urlparse(scrape.url or "").hostname or ""
    if not hostname:
        return
    if hostname.startswith("www."):
        hostname = hostname[4:]

    content_len = len(scrape.job_content or "")
    outcome_value = 1.0 if success else 0.0

    profile, created = ScrapeProfile.objects.get_or_create(
        hostname=hostname,
        defaults={
            "requires_auth": False,
            "avg_content_length": content_len,
            "success_rate": outcome_value,
            "scrape_count": 1,
            "failure_count": 0 if success else 1,
            "tier0_miss_count": 1 if tier0_hit is False else 0,
            "last_success_at": timezone.now() if success else None,
            "created_by": user,
        },
    )

    if not created:
        prev_count = profile.scrape_count
        profile.scrape_count = prev_count + 1
        if success:
            profile.last_success_at = timezone.now()
        else:
            profile.failure_count = (profile.failure_count or 0) + 1
        if tier0_hit is False:
            profile.tier0_miss_count = (profile.tier0_miss_count or 0) + 1

        if profile.avg_content_length:
            profile.avg_content_length = int(
                (profile.avg_content_length * prev_count + content_len)
                / profile.scrape_count
            )
        else:
            profile.avg_content_length = content_len
        profile.success_rate = max(
            0.0,
            min(
                1.0,
                (profile.success_rate * prev_count + outcome_value) / profile.scrape_count,
            ),
        )

        # Auto-demote Tier 0 when selectors stop matching often enough.
        # This also (intentionally) flips ScrapeProfile.is_known_good False —
        # a demoted host runs at tier "1", which is outside the known-good
        # allowed tiers. Demotion on sustained tier-0 misses wins over
        # promotion; the counters this method maintains (scrape_count /
        # success_rate / tier0_miss_count) are exactly what is_known_good reads,
        # so a recovered host flips back to known-good with no extra write path.
        if (
            profile.preferred_tier == "auto"
            and profile.tier0_miss_count >= _TIER0_DEMOTE_MIN_MISSES
            and profile.tier0_miss_count / profile.scrape_count >= _TIER0_DEMOTE_MISS_RATIO
        ):
            logger.warning(
                "Auto-demoting %s from tier=auto → tier=1 "
                "(tier0_miss_count=%d/%d)",
                hostname, profile.tier0_miss_count, profile.scrape_count,
            )
            profile.preferred_tier = "1"

        profile.save()

    logger.info(
        "Scrape profile %s for %s (count=%d, fail=%d, miss=%d, rate=%.0f%%, tier=%s)",
        "created" if created else "updated",
        hostname,
        profile.scrape_count,
        profile.failure_count or 0,
        profile.tier0_miss_count or 0,
        profile.success_rate * 100,
        profile.preferred_tier,
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
    css_selectors: dict = Field(
        default_factory=dict,
        description="CSS selectors for job data fields based on common patterns for this "
        "domain. Keys should be: title, company_name, description, location, salary. "
        "Values are CSS selector strings. Only include selectors you are confident about "
        "based on the domain's typical job page structure. Omit fields you are unsure of."
    )


def _generate_profile_hints(profile, scrape, user=None):
    """Use a cheap LLM call to generate extraction_hints, page_structure, and CSS selectors for a ScrapeProfile."""
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

    prompt = f"""Analyze this scraped job posting content from {profile.hostname} and describe extraction patterns, page structure, and suggest CSS selectors.

For css_selectors, suggest selectors based on common patterns used by {profile.hostname} for job pages. Common patterns include class-based selectors like ".job-title", ID selectors like "#job-description", or data attributes like "[data-testid='title']". Only include selectors you are reasonably confident about for this domain. Omit fields where you cannot make a good guess.

Content (first 2000 chars):
{content[:2000]}"""

    try:
        result = agent.run_sync(prompt)
        hints = result.output

        profile.extraction_hints = hints.extraction_hints[:1000]
        profile.page_structure = hints.page_structure[:1000]

        # Save proposed CSS selectors under job_data if none exist yet
        existing_selectors = profile.css_selectors or {}
        if hints.css_selectors and not existing_selectors.get("job_data"):
            existing_selectors["job_data"] = hints.css_selectors
            profile.css_selectors = existing_selectors

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
