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
import re
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


# A description that is nothing but a bracketed "not captured" marker —
# e.g. "[DESCRIPTION NOT CAPTURED — LinkedIn page rendered only the top
# card; rescrape later or capture via the cc_sender extension]", the
# exact wording migration 0101 installs in the linkedin.com
# ScrapeProfile.extraction_hints and the agents/ scrape-graph now
# recognizes as a stub.
#
# Why this needs its own gate: the marker is ~120 non-empty characters,
# so it sails past the `if not desc` short-circuit below and lands on
# the LLM — whose prompt has never heard of the sentinel and is told to
# "default to ACCEPT when uncertain". Live evidence: jp rHeRo6qWCG
# carries exactly this description and still has complete=True. A
# refusal is only as good as its survival through persistence.
#
# Anchored to the whole string on purpose. A real description that
# happens to quote the marker mid-body is a real description.
_NOT_CAPTURED_SENTINEL = re.compile(
    r"^\s*\[[^\]]*\bnot\s+captured\b[^\]]*\]\s*$",
    flags=re.IGNORECASE,
)


def is_not_captured_sentinel(description: Optional[str]) -> bool:
    """True when ``description`` is only a "not captured" placeholder."""
    return bool(_NOT_CAPTURED_SENTINEL.match(description or ""))


def description_is_refusal(description: Optional[str]) -> bool:
    """True when a description cannot honestly carry ``complete=True``.

    Two shapes, both decided deterministically — no LLM, no network:

    - nothing at all (empty or whitespace-only);
    - a labelled "not captured" placeholder, which is a refusal *in
      writing* and must never be read as content.

    This is exactly the cheap pre-gate ``maybe_review_and_persist``
    already ran, lifted out into a predicate so the persistence path can
    apply the same verdict at the moment it writes the row (CC-237).
    Until now the verdict only existed downstream of an LLM call that
    several ingest paths never make: ``ScrapeViewSet._persist_extension
    _direct`` calls ``process_evaluation`` and marks the scrape completed
    without ever reviewing, so a description-less capture minted a
    ``complete=True`` row that nothing would afterwards correct.

    What this does NOT cover, on purpose: semantic junk — a plausible
    description synthesised from an application form's own questions.
    That needs a reader and stays the LLM reviewer's job. This predicate
    only covers the cases where the pipeline has already said, in the
    data it persisted, that it found nothing.
    """
    text = (description or "").strip()
    return not text or is_not_captured_sentinel(text)


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
    sentinel = is_not_captured_sentinel(desc)
    if description_is_refusal(desc):
        # Empty descriptions don't need an LLM judgement — there's
        # literally nothing to evaluate. Neither does a bare "not
        # captured" marker: the extractor has already told us, in
        # writing, that it found no description. Both mean the same
        # thing to every caller — complete=False, re-trigger a scrape.
        # Short-but-non-empty descriptions fall through to the LLM
        # so a plausible 30-word stub gets a fair read; the cost of
        # a false-reject (annoying) outweighs paying for the call.
        if job_post.complete:
            job_post.complete = False
            job_post.save(update_fields=["complete"])
        return ReviewDecision(
            looks_like_job_description=False,
            confidence="high",
            reasoning=(
                "Description is a 'not captured' placeholder; skipped LLM."
                if sentinel
                else "Description is empty; skipped LLM."
            ),
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
