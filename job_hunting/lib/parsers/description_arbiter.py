"""DescriptionArbiter — chooses between two competing job-post descriptions.

Called when process_evaluation hits the `duplicate` / `duplicate_via_fingerprint`
path and both the existing and incoming descriptions are non-thin. The arbiter
uses a cheap Jaccard gate first (skip LLM when descriptions are nearly identical)
then falls back to an LLM structured-output call when they differ materially.

Every invocation is persisted as a ``JobPostDescriptionDecision`` audit row so
the choices can be reviewed / replayed.

Controlled by two env vars:

    INGEST_ARBITER_ENABLED   — "false" disables entirely (default: enabled)
    DESCRIPTION_ARBITER_MODEL — provider:model spec (default: openai:gpt-4o-mini)
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "openai:gpt-4o-mini"

_SYSTEM_PROMPT = """\
You are a job-description quality judge. You will be shown two versions of a
description for the same job posting and must decide which to keep in a
job-hunt management tool.

Your output must be one of:
  keep_existing — the existing version is better or equivalent
  use_new       — the incoming version is materially better
  merge         — both have unique, valuable content worth combining

QUALITY SIGNALS (apply these):
• Penalise descriptions dominated by UI chrome: "Apply", "Save", "Sign in",
  "Promoted by hirer", "Easy Apply", "Show more", share/like/follow links,
  pagination text, cookie banners.
• Reward substantive prose that contains at least some of: responsibilities,
  qualifications or requirements, compensation, tech stack, team context.
• If one version has chrome at the top/bottom but substantive prose in the
  middle, the prose still counts — strip the chrome in your quality assessment.
• Prefer completeness: more relevant content beats less, all else equal.

MERGE RULES:
• Only choose "merge" when each version has distinct RELEVANT content the other lacks.
• The merged_description must be plain text (no markdown headers). Combine the
  substantive prose from both sources; drop duplicate sentences and all chrome.
• Never invent job details not present in either source.
• Keep merged_description under 8 000 words.

DEFAULT: when uncertain, return keep_existing with confidence "low".
"""

_USER_TEMPLATE = """\
JOB TITLE: {title}
COMPANY: {company_name}

--- EXISTING DESCRIPTION ---
source: {existing_source}
link: {existing_link}

{existing_description}

--- INCOMING DESCRIPTION ---
source: {new_source}
link: {new_link}

{new_description}
"""


class ArbitrationDecision(BaseModel):
    choice: Literal["keep_existing", "use_new", "merge"]
    confidence: Literal["high", "medium", "low"]
    reasoning: str = Field(..., max_length=500)
    merged_description: Optional[str] = Field(None, max_length=20_000)

    @model_validator(mode="after")
    def merged_required_when_merge(self) -> "ArbitrationDecision":
        if self.choice == "merge" and not self.merged_description:
            raise ValueError("merged_description is required when choice is 'merge'")
        if self.choice != "merge" and self.merged_description:
            self.merged_description = None
        return self


class DescriptionArbiter:
    def __init__(self, model: Optional[str] = None):
        self._model_spec = (
            model
            or os.environ.get("DESCRIPTION_ARBITER_MODEL")
            or _DEFAULT_MODEL
        )

    @property
    def model_spec(self) -> str:
        return self._model_spec

    def arbitrate(
        self,
        *,
        title: str,
        company_name: str,
        existing_description: str,
        existing_link: str,
        existing_source: str,
        new_description: str,
        new_link: str,
        new_source: str,
    ) -> ArbitrationDecision:
        """Compare two descriptions and return a structured decision.

        Applies the Jaccard cheapness gate first — when the 5-gram overlap
        is high and word-count ratio is close, the descriptions are
        effectively identical and we skip the LLM.
        """
        from job_hunting.lib.text_signals import jaccard_5gram

        overlap = jaccard_5gram(existing_description, new_description)
        existing_words = len(existing_description.split())
        new_words = len(new_description.split())
        ratio = max(existing_words, new_words) / max(min(existing_words, new_words), 1)

        if overlap > 0.70 and ratio < 1.5:
            logger.debug(
                "DescriptionArbiter: cheapness gate hit (jaccard=%.2f ratio=%.2f) — "
                "keeping existing",
                overlap,
                ratio,
            )
            return ArbitrationDecision(
                choice="keep_existing",
                confidence="high",
                reasoning=(
                    f"Descriptions are {overlap:.0%} similar by 5-gram Jaccard "
                    f"(word-count ratio {ratio:.1f}) — effectively identical."
                ),
                merged_description=None,
            )

        return self._call_llm(
            title=title,
            company_name=company_name,
            existing_description=existing_description,
            existing_link=existing_link,
            existing_source=existing_source,
            new_description=new_description,
            new_link=new_link,
            new_source=new_source,
        )

    def _call_llm(self, **kwargs) -> ArbitrationDecision:
        from pydantic_ai import Agent
        from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
        from pydantic_ai.providers.ollama import OllamaProvider

        spec = self._model_spec
        if ":" not in spec:
            raise ValueError(
                f"DESCRIPTION_ARBITER_MODEL {spec!r} must use 'provider:model' form."
            )
        provider, bare = spec.split(":", 1)
        if provider == "ollama":
            model = OpenAIChatModel(
                model_name=bare,
                provider=OllamaProvider(
                    base_url=os.environ.get("OLLAMA_API_BASE", "http://localhost:11434/v1")
                ),
            )
        elif provider == "anthropic":
            from pydantic_ai.models.anthropic import AnthropicModel
            model = AnthropicModel(bare)
        elif provider == "openai":
            model = OpenAIResponsesModel(bare)
        else:
            raise ValueError(f"Unknown provider {provider!r} in arbiter model spec.")

        agent = Agent(model, output_type=ArbitrationDecision, system_prompt=_SYSTEM_PROMPT)
        prompt = _USER_TEMPLATE.format(
            title=kwargs["title"],
            company_name=kwargs["company_name"],
            existing_source=kwargs["existing_source"] or "unknown",
            existing_link=kwargs["existing_link"] or "",
            existing_description=kwargs["existing_description"],
            new_source=kwargs["new_source"] or "unknown",
            new_link=kwargs["new_link"] or "",
            new_description=kwargs["new_description"],
        )
        result = agent.run_sync(prompt)
        return result.output


def _sha256_truncate(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def maybe_arbitrate_and_persist(
    *,
    job_post,
    scrape,
    new_description: str,
    new_source: str,
    new_link: str,
) -> Optional[str]:
    """Run the arbiter against job_post.description vs new_description.

    Returns the winning description (may be the existing, the new, or a
    merged version). Returns None when arbitration is disabled or skipped.
    Persists a JobPostDescriptionDecision audit row regardless of outcome.
    """
    if os.environ.get("INGEST_ARBITER_ENABLED", "true").lower() in ("false", "0", "no"):
        return None

    existing_description = (job_post.description or "").strip()
    new_description = (new_description or "").strip()
    if not existing_description or not new_description:
        return None

    arbiter = DescriptionArbiter()
    t0 = time.monotonic()
    error_occurred = False
    decision = None
    try:
        decision = arbiter.arbitrate(
            title=job_post.title or "",
            company_name=(
                job_post.company.name if job_post.company_id else ""
            ),
            existing_description=existing_description,
            existing_link=job_post.link or "",
            existing_source=job_post.source or "",
            new_description=new_description,
            new_link=new_link or "",
            new_source=new_source or "",
        )
    except Exception:
        logger.exception(
            "DescriptionArbiter: LLM call failed for job_post=%s", job_post.pk
        )
        error_occurred = True
        decision = ArbitrationDecision(
            choice="keep_existing",
            confidence="low",
            reasoning="Arbiter LLM call failed — keeping existing as safe fallback.",
            merged_description=None,
        )

    duration_ms = int((time.monotonic() - t0) * 1000)

    try:
        from job_hunting.models.job_post_description_decision import (
            JobPostDescriptionDecision,
        )

        JobPostDescriptionDecision.objects.create(
            job_post=job_post,
            triggering_scrape=scrape,
            existing_description_hash=_sha256_truncate(existing_description),
            new_description_hash=_sha256_truncate(new_description),
            existing_word_count=len(existing_description.split()),
            new_word_count=len(new_description.split()),
            existing_source=job_post.source or "",
            new_source=new_source or "",
            choice=decision.choice,
            confidence=decision.confidence,
            reasoning=decision.reasoning or "",
            model_name="" if error_occurred else arbiter.model_spec,
            duration_ms=duration_ms,
        )
    except Exception:
        logger.exception(
            "DescriptionArbiter: failed to persist audit row for job_post=%s",
            job_post.pk,
        )

    logger.info(
        "DescriptionArbiter: job_post=%s choice=%s confidence=%s duration_ms=%d",
        job_post.pk,
        decision.choice,
        decision.confidence,
        duration_ms,
    )

    if decision.choice == "use_new":
        return new_description
    if decision.choice == "merge" and decision.merged_description:
        return decision.merged_description
    return existing_description
