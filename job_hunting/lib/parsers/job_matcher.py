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
clean mock seam. No retries — one LLM call per invocation is the cost guardrail.

Controlled by:

    JOB_MATCHER_MODEL — provider:model spec (default: openai:gpt-4o-mini)
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "openai:gpt-4o-mini"

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
        """
        return self._call_llm(
            url=url,
            referrer=referrer,
            page_title=page_title,
            text_excerpt=text_excerpt,
            candidates=candidates,
        )

    def _call_llm(self, **kwargs) -> MatchDecision:
        from pydantic_ai import Agent
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

        agent = Agent(model, output_type=MatchDecision, system_prompt=_SYSTEM_PROMPT)
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
