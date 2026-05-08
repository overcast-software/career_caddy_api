"""CompletenessReviewer — does this JobPost actually look like a job description?

Runs as a final gate after parse_scrape successfully fills/upgrades a JP.
A cheap LLM (Haiku-class default) is shown the persisted title +
company + description + link and answers a single yes/no: does this
read like a real job posting?

Pass → leave JobPost.complete alone (parse_scrape already flipped it
to True on the upgrade paths).
Fail → flip JobPost.complete=False so the extension popup will offer
"Send this page" the next time the user lands on that URL, and the
from-text dedup bypass lets a fresh scrape through.

The JobPost row stays in place either way — we never delete or hide
the post on a failed review. The flag is the load-bearing signal.

Env vars:
  COMPLETENESS_REVIEWER_ENABLED  — "false" disables entirely (default: enabled)
  COMPLETENESS_REVIEWER_MODEL    — provider:model spec (default: anthropic:claude-haiku-4-5)

The same review logic will eventually live as a `ReviewCompleteness`
node in the agents/ scrape-graph (Phase 1d). For now the production
hook is parse_scrape calling maybe_review_and_persist directly so the
gate fires today instead of waiting for the graph to ship.
"""
from __future__ import annotations

import logging
import os
from typing import Literal, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "anthropic:claude-haiku-4-5"

_SYSTEM_PROMPT = """\
You are a final-gate quality judge for a job-hunt management tool. You
will be shown one extracted job-post record (title, company,
description, link) and must answer a single question: does this look
like a genuine job posting?

REJECT (looks_like_job_description=false) when:
- The description is empty, near-empty, or just UI chrome ("Apply",
  "Save", "Sign in", "Show more", cookie banners, share/follow links).
- The text is clearly not a job posting — a search-results page, a
  company landing page, a 404/login wall, a paywall, a generic "we're
  hiring" recruiting page with no actual role.
- Title and description are wildly inconsistent (e.g. title says
  "Senior Engineer" but description is about an unrelated product).
- The description contains the role's name plus pure boilerplate
  ("Join our team! Apply today!") with zero substance.

ACCEPT (looks_like_job_description=true) when:
- The description contains real responsibilities, qualifications,
  team context, tech stack, compensation, OR substantive prose about
  the role even if some sections are missing. A short but legitimate
  job description is still a job description.
- Light UI chrome around substantive content is fine — the prose in
  the middle is what matters.

Default to ACCEPT when uncertain (confidence=low). Only REJECT with
confidence=medium or higher. The cost of a false reject is annoying;
the cost of a false accept is the user not knowing the post is junk.
"""

_USER_TEMPLATE = """\
JOB TITLE: {title}
COMPANY: {company_name}
LINK: {link}

--- DESCRIPTION ---
{description}
"""


class ReviewDecision(BaseModel):
    looks_like_job_description: bool
    confidence: Literal["high", "medium", "low"]
    reasoning: str = Field(..., max_length=500)


def _enabled() -> bool:
    return os.environ.get("COMPLETENESS_REVIEWER_ENABLED", "true").lower() != "false"


class CompletenessReviewer:
    def __init__(self, model: Optional[str] = None):
        self._model_spec = (
            model
            or os.environ.get("COMPLETENESS_REVIEWER_MODEL")
            or _DEFAULT_MODEL
        )

    @property
    def model_spec(self) -> str:
        return self._model_spec

    def review(
        self,
        *,
        title: Optional[str],
        company_name: Optional[str],
        description: Optional[str],
        link: Optional[str],
    ) -> ReviewDecision:
        """Return the LLM's verdict. The cheap pre-gate (empty/near-empty
        description) lands in maybe_review_and_persist, not here, so this
        method is a pure LLM call and easy to unit-test with mocks."""
        return self._call_llm(
            title=title or "",
            company_name=company_name or "",
            description=description or "",
            link=link or "",
        )

    def _call_llm(self, **kwargs) -> ReviewDecision:
        from pydantic_ai import Agent
        from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
        from pydantic_ai.providers.ollama import OllamaProvider

        spec = self._model_spec
        if ":" not in spec:
            raise ValueError(
                f"COMPLETENESS_REVIEWER_MODEL {spec!r} must use 'provider:model' form."
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
            raise ValueError(f"Unknown provider {provider!r} in reviewer model spec.")

        agent = Agent(model, output_type=ReviewDecision, system_prompt=_SYSTEM_PROMPT)
        prompt = _USER_TEMPLATE.format(
            title=kwargs["title"],
            company_name=kwargs["company_name"],
            description=kwargs["description"],
            link=kwargs["link"],
        )
        result = agent.run_sync(prompt)
        return result.output


# Outcomes from JobPostExtractor.process_evaluation that mean the JP's
# description was just (re)written. These are the ones worth reviewing.
# Pure dedup hits ("duplicate", "force_noop") didn't change the
# description, so re-running the LLM on them is cost without signal.
_REVIEWABLE_OUTCOMES = {
    "created",
    "updated_stub",
    "updated_stub_via_fingerprint",
    "force_updated",
}


def maybe_review_and_persist(
    job_post,
    *,
    last_outcome: Optional[str] = None,
    reviewer: Optional[CompletenessReviewer] = None,
) -> Optional[ReviewDecision]:
    """Final gate after a successful scrape attach. Flips
    JobPost.complete=False when the LLM rejects the output.

    Skipped when:
    - The reviewer is disabled via env.
    - last_outcome is None or wasn't a description-write path
      (duplicate / force_noop hits aren't reviewable).
    - The description is already empty or essentially empty — the
      heuristic catches this without paying for an LLM call. The post
      is flipped to complete=False directly.

    Returns the decision (or None when skipped) so callers can audit
    or test.
    """
    if not _enabled():
        logger.debug("CompletenessReviewer disabled via env; skipping JP %s", job_post.id)
        return None
    if last_outcome is not None and last_outcome not in _REVIEWABLE_OUTCOMES:
        return None

    desc = (job_post.description or "").strip()
    if not desc or len(desc) < 50:
        # Empty/near-empty descriptions don't need an LLM judgement.
        # The user / agent / extension caller will see complete=False
        # and re-trigger a scrape.
        if job_post.complete:
            job_post.complete = False
            job_post.save(update_fields=["complete"])
        return ReviewDecision(
            looks_like_job_description=False,
            confidence="high",
            reasoning="Description is empty or shorter than 50 chars; skipped LLM.",
        )

    decision = (reviewer or CompletenessReviewer()).review(
        title=job_post.title,
        company_name=(job_post.company.name if job_post.company_id else None),
        description=job_post.description,
        link=job_post.link,
    )
    if not decision.looks_like_job_description and job_post.complete:
        job_post.complete = False
        job_post.save(update_fields=["complete"])
        logger.info(
            "CompletenessReviewer rejected JP %s (confidence=%s): %s",
            job_post.id,
            decision.confidence,
            decision.reasoning,
        )
    return decision
