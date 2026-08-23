"""JobMatcher — picks the JobPost that a live application page belongs to.

Backs the CC-135 staff-gated agentic lookup. Given the application CONTEXT a
``JobApplication.match_context`` carries (url, referrer, page_title,
text_excerpt) plus a pre-fetched list of candidate JobPosts, the matcher makes
ONE structured LLM call that either picks exactly one candidate id or returns
null. It is a CHOOSE-FROM-LIST decision: the model may only return an id
present in the candidate list, and a returned id that isn't in the list is
treated as null by the caller (``job_application_match_job``). It never invents
an id and never scores a post that wasn't handed to it.

Mirrors ``DescriptionArbiter`` (same directory): a pydantic-ai ``Agent`` with a
pydantic ``output_type`` for structured output, wrapped so ``_call_llm`` is a
clean mock seam. One retry (CC-240), spent only when the model contradicts
itself by picking a post at a company it just said the page is not for — see
the ``output_validator`` in ``_call_llm``. Everything else is one LLM call.

Controlled by:

    JOB_MATCHER_MODEL — provider:model spec (default: openai:gpt-5)

Resolution order is the explicit ``model`` argument, then JOB_MATCHER_MODEL,
then ``_DEFAULT_MODEL``. Note CADDY_DEFAULT_MODEL does NOT apply here: this
role carries its own default, and the ``job_matcher`` row in
``job_hunting.api.views.admin._agent_role_specs`` hardcodes the same value so
the admin surface reports what actually resolves. Change one, change both —
``tests/test_agent_model_registry.py`` fails if they drift.
"""
from __future__ import annotations

import logging
import os
import re
from typing import List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# CC-240: was openai:gpt-4o-mini. This task is mostly RESTRAINT — knowing when
# to return null — which is where a small model is weakest, and it showed: on
# 2026-08-12 the matcher tied a Block application to a Golden Analytics job
# post at 0.9 confidence, its own rationale admitting a different company and a
# different title, justified by "hosted on the same platform link".
_DEFAULT_MODEL = "openai:gpt-5"

# A pick this weak is worse than no pick — the result is shown to the user as a
# finding, and a wrong one is tracked against the wrong job and feeds the wrong
# company into generated answers. Below this, return null.
#
# This stays a deterministic POLICY rather than a ModelRetry: telling a model
# "you're not confident enough, try again" just teaches it to inflate the
# number. Low confidence is a legitimate answer; we simply decline to act on it.
MATCH_CONFIDENCE_FLOOR = 0.5


def _same_company(a: str, b: str) -> bool:
    """Loose equality between two company NAMES.

    Deliberately tiny. An earlier version scanned the whole page for the
    company using a legal-suffix table ("inc", "llc", "gmbh", …) and a
    minimum-token-length rule — a bespoke entity matcher that would have
    survived exactly the one case it was written for.

    Comparing two short names the model itself produced is a far smaller
    problem than comparing a name against an entire page, so casefolded
    alphanumeric containment is enough to separate "Block" from
    "Golden Analytics". It does NOT solve aliasing (Block/Square,
    Meta/Facebook) and is not trying to: that belongs to a Company lookup,
    not to string surgery.
    """
    na = re.sub(r"[^a-z0-9]", "", (a or "").lower())
    nb = re.sub(r"[^a-z0-9]", "", (b or "").lower())
    if not na or not nb:
        return True  # nothing to contradict
    return na in nb or nb in na

_SYSTEM_PROMPT = """\
You are a job-application matcher. You will be shown the CONTEXT of a job
application page a user is currently on (its URL, the page they arrived from,
the visible page title, and an excerpt of the page body), plus a numbered list
of CANDIDATE job posts already in the user's corpus.

Your job: decide which single candidate — if any — is the SAME job the
application page is for.

RULES:
• You may ONLY return the id of a candidate in the list, or null. Never invent
  an id. Never return an id that is not in the candidate list.
• Return null when no candidate is clearly the same job. A weak or speculative
  match is worse than null here — the result is offered to the user, so a wrong
  pick erodes trust.
• Match on the SUBSTANCE (same role at the same company), not superficial URL
  or wording similarity. The same job is often reworded across boards and ATSes.
• A candidate whose posting host matches the referrer host, or whose apply
  destination matches the application URL, is a strong signal — but confirm the
  role + company agree before picking it.
• application_company is the company the APPLICATION PAGE is for, exactly as
  the page names it — decide this FIRST, before looking at the candidates, and
  report it whether or not you pick anything. Empty string only if the page
  genuinely never names a company.
• A candidate at a DIFFERENT company than application_company is never the
  same job, no matter how similar the title or how alike the URLs. Shared
  hosting is not shared identity: two companies both using the same applicant
  tracking system tells you nothing about whether the roles are the same.
• confidence is your own 0-1 estimate that the pick is correct. Use lower
  values when the context is thin. On null, confidence should be low.
• rationale is ONE sentence explaining the decision.
"""

_USER_TEMPLATE = """\
APPLICATION PAGE CONTEXT
url: {url}
referrer: {referrer}
page_title: {page_title}

page excerpt:
{text_excerpt}

CANDIDATE JOB POSTS
{candidates}
"""


class MatchDecision(BaseModel):
    """Structured output of one matcher call."""

    job_post_id: Optional[str] = Field(
        None,
        description="id of the chosen candidate, or null when none matches",
    )
    # CC-240: the model must NAME the company the application page is for,
    # separately from its pick. That turns "did it match the right company?"
    # into a self-consistency check the schema can enforce — the model
    # contradicting itself is detectable without any page-scanning heuristics,
    # and it is visible in the output when it happens.
    application_company: str = Field(
        "",
        description=(
            "the company the APPLICATION PAGE is for, exactly as the page "
            "names it; empty string only if the page never says"
        ),
    )
    confidence: float = Field(..., ge=0.0, le=1.0)
    rationale: str = Field(..., max_length=500)


class CandidatePost(BaseModel):
    """Compact candidate the matcher reasons over — NOT the full JobPost.

    Only the fields the model needs to judge sameness, keeping the prompt (and
    token cost) small. Built by ``job_application_match_job`` from the visible
    posts.
    """

    id: str
    title: str = ""
    company: str = ""
    link_host: str = ""
    created: str = ""


class JobMatcher:
    def __init__(self, model: Optional[str] = None):
        self._model_spec = (
            model
            or os.environ.get("JOB_MATCHER_MODEL")
            or _DEFAULT_MODEL
        )

    @property
    def model_spec(self) -> str:
        return self._model_spec

    def match(
        self,
        *,
        url: str,
        referrer: str,
        page_title: str,
        text_excerpt: str,
        candidates: List[CandidatePost],
    ) -> MatchDecision:
        """Run one LLM call to pick a candidate id (or null).

        Callers must pre-filter to a non-empty candidate list — the
        zero-candidate short-circuit lives in ``job_application_match_job`` so
        no LLM call is spent when there is nothing to choose from.

        The model's answer is then VERIFIED deterministically — see
        ``_verify``. The prompt already forbids cross-company picks and the
        model made one anyway, so the check cannot live in the prompt.
        """
        decision = self._call_llm(
            url=url,
            referrer=referrer,
            page_title=page_title,
            text_excerpt=text_excerpt,
            candidates=candidates,
        )
        return self._apply_confidence_floor(decision)

    def _apply_confidence_floor(self, decision: MatchDecision) -> MatchDecision:
        """Decline to act on a pick the model isn't confident about. CC-240.

        The ONLY post-check left. Cross-company picks are now rejected inside
        the agent by an output validator (see ``_call_llm``), which hands the
        contradiction back to the model to fix rather than silently correcting
        it out here.

        This one stays deterministic and stays out of the model's hands on
        purpose: "you are not confident enough, try again" would just teach it
        to inflate the number. Low confidence is a legitimate answer — we
        simply decline to present it as a finding.

        Confidence is preserved on the returned null so callers can log how
        sure the model was about a pick we threw away. That is the dataset
        showing whether the model or the threshold needs to move.
        """
        if not decision.job_post_id:
            return decision
        if decision.confidence >= MATCH_CONFIDENCE_FLOOR:
            return decision

        logger.info(
            "job_matcher: dropping candidate %s — confidence %.2f below floor %.2f",
            decision.job_post_id,
            decision.confidence,
            MATCH_CONFIDENCE_FLOOR,
        )
        return MatchDecision(
            job_post_id=None,
            application_company=decision.application_company,
            confidence=decision.confidence,
            rationale=(
                f"[no confident match: below the {MATCH_CONFIDENCE_FLOOR} "
                f"confidence floor] {decision.rationale}"
            )[:500],
        )

    def _call_llm(self, **kwargs) -> MatchDecision:
        from pydantic_ai import Agent, ModelRetry
        from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
        from pydantic_ai.providers.ollama import OllamaProvider

        spec = self._model_spec
        if ":" not in spec:
            raise ValueError(
                f"JOB_MATCHER_MODEL {spec!r} must use 'provider:model' form."
            )
        provider, bare = spec.split(":", 1)
        if provider == "ollama":
            model = OpenAIChatModel(
                model_name=bare,
                provider=OllamaProvider(
                    base_url=os.environ.get(
                        "OLLAMA_API_BASE", "http://localhost:11434/v1"
                    )
                ),
            )
        elif provider == "anthropic":
            from pydantic_ai.models.anthropic import AnthropicModel
            model = AnthropicModel(bare)
        elif provider == "openai":
            model = OpenAIResponsesModel(bare)
        else:
            raise ValueError(f"Unknown provider {provider!r} in matcher model spec.")

        # CC-240: one retry, spent only on a self-contradiction. The original
        # "no retries" rule was a cost guardrail; a retry that fires solely
        # when the model picks a company it just told us the page is NOT for
        # is the cheapest possible place to spend a second call.
        agent = Agent(
            model,
            output_type=MatchDecision,
            system_prompt=_SYSTEM_PROMPT,
            retries=1,
        )

        by_id = {str(c.id): c for c in kwargs["candidates"]}

        @agent.output_validator
        def _reject_cross_company(decision: MatchDecision) -> MatchDecision:
            """Enforce the rule the prompt already stated and the model ignored.

            The model reports which company the APPLICATION PAGE is for. If it
            then picks a post at a different company, that is a contradiction
            in its own output — no page re-reading, no entity-matching
            heuristics, no table of legal suffixes. ModelRetry hands the
            contradiction back so it can correct itself; the previous design
            silently nulled the answer and taught it nothing.
            """
            if not decision.job_post_id:
                return decision
            chosen = by_id.get(str(decision.job_post_id))
            # Off-list ids are the caller's to handle (job_application_match_job).
            if chosen is None:
                return decision
            if _same_company(chosen.company, decision.application_company):
                return decision
            raise ModelRetry(
                f"You reported the application page is for "
                f"{decision.application_company!r}, then chose a job post at "
                f"{chosen.company!r}. Those are different companies. Shared "
                f"hosting or a similar-sounding title is not the same job — "
                f"pick a candidate at {decision.application_company!r} or "
                f"return null."
            )

        candidate_lines = "\n".join(
            f"- id={c.id} | title={c.title or 'unknown'} | "
            f"company={c.company or 'unknown'} | host={c.link_host or 'unknown'} | "
            f"posted={c.created or 'unknown'}"
            for c in kwargs["candidates"]
        )
        prompt = _USER_TEMPLATE.format(
            url=kwargs["url"] or "unknown",
            referrer=kwargs["referrer"] or "none",
            page_title=kwargs["page_title"] or "unknown",
            text_excerpt=kwargs["text_excerpt"] or "",
            candidates=candidate_lines,
        )
        result = agent.run_sync(prompt)
        return result.output
