import os

from typing import Optional
from datetime import datetime
from job_hunting.lib.models import CareerData
from jinja2 import Environment, FileSystemLoader

# How much of any ONE section survives into the prompt.
#
# Env-overridable because it is a tuning knob, not a design decision, and
# tuning it should not need a deploy — the same reasoning that keeps
# ScrapeProfile in JSONB. `ANSWER_MODEL` next door already establishes the
# pattern for this role.
#
# Raised from a hardcoded 60,000 on Doug's call, 2026-08-25, so a long career
# profile is not silently clipped mid-history. Note what this does NOT fix:
# a bigger profile makes the user's own directive a smaller FRACTION of the
# prompt, which is the opposite of what he also asked for. That is why the
# restatement at the end of `build()` ships in the same change — raising this
# alone would have made the reported symptom worse.
MAX_SECTION_CHARS_ENV = "ANSWER_MAX_SECTION_CHARS"
MAX_SECTION_CHARS_DEFAULT = 120000


def resolve_max_section_chars() -> int:
    """The per-section cap, from the environment or the default.

    A bad value falls back rather than raising: a typo in an env var must not
    take down answer generation, and the default is always a safe answer.
    """
    raw = os.environ.get(MAX_SECTION_CHARS_ENV)
    if not raw:
        return MAX_SECTION_CHARS_DEFAULT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return MAX_SECTION_CHARS_DEFAULT
    return value if value > 0 else MAX_SECTION_CHARS_DEFAULT


class ApplicationPromptBuilder:
    """
    ApplicationPromptBuilder instantiates a career-data
    and ports it to markdown
    build: generic build
    build_from_career_data uses career-data
    """

    def __init__(self, max_section_chars=None):
        self.max_section_chars = (
            max_section_chars
            if max_section_chars is not None
            else resolve_max_section_chars()
        )

    def _truncate(self, text, max_chars):
        if not text:
            return ""
        text = str(text)
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "..."

    def _safe_str(self, val):
        if val is None:
            return ""
        return str(val).strip()

    def _resume_text(self, resume):
        env = Environment(loader=FileSystemLoader("templates"), autoescape=False)  # nosec B701 - text/LLM prompt templates, not HTML
        template = env.get_template("resume_markdown.j2")
        if not resume:
            raise ValueError("Resume cannot be None")

        return template.render(resume=resume)

    def _job_post_text(self, job_post):
        if not job_post:
            return ""

        parts = []

        title = self._safe_str(getattr(job_post, "title", ""))
        if title:
            parts.append(f"Title: {title}")

        if hasattr(job_post, "company") and job_post.company:
            company_name = self._safe_str(getattr(job_post.company, "name", ""))
            if company_name:
                parts.append(f"Company: {company_name}")

        posted_date = getattr(job_post, "posted_date", None)
        if posted_date:
            parts.append(f"Posted: {posted_date}")

        link = self._safe_str(getattr(job_post, "link", ""))
        if link:
            parts.append(f"Link: {link}")

        description = self._safe_str(getattr(job_post, "description", ""))
        if description:
            parts.append(
                f"Description: {self._truncate(description, self.max_section_chars)}"
            )

        return "\n".join(parts)

    def _company_text(self, company):
        if not company:
            return ""

        parts = []
        name = self._safe_str(getattr(company, "name", ""))
        if name:
            parts.append(f"Name: {name}")

        display_name = self._safe_str(getattr(company, "display_name", ""))
        if display_name and display_name != name:
            parts.append(f"Display Name: {display_name}")

        parts.append(f"Company Notes: {company.notes or 'None'}")
        return "\n".join(parts)

    def _cover_letters_text(self, cover_letters):
        if not cover_letters:
            return ""

        lines = []
        per_item_budget = max(
            3000, self.max_section_chars // max(1, len(cover_letters))
        )

        for letter in cover_letters:
            header_parts = []

            # Add creation date
            created_at = getattr(letter, "created_at", None)
            if created_at:
                header_parts.append(f"Created: {created_at.strftime('%Y-%m-%d')}")

            # Add job post title and company
            if hasattr(letter, "job_post") and letter.job_post:
                job_title = self._safe_str(getattr(letter.job_post, "title", ""))
                if job_title:
                    header_parts.append(f"Job: {job_title}")

                if hasattr(letter.job_post, "company") and letter.job_post.company:
                    company_name = self._safe_str(
                        getattr(letter.job_post.company, "name", "")
                    )
                    if company_name:
                        header_parts.append(f"Company: {company_name}")

            header = " | ".join(header_parts) if header_parts else "Cover Letter"
            content = self._truncate(getattr(letter, "content", ""), per_item_budget)

            if content:
                lines.append(f"{header}")
                lines.append(content)
                lines.append("")  # Empty line between letters

        return "\n".join(lines).strip()

    def _qas_text(self, qas):
        if not qas:
            return ""
        lines = []
        for qa in qas:
            q_text = qa.get("question", "") if isinstance(qa, dict) else getattr(getattr(qa, "question", None), "content", "")
            a_text = qa.get("answer", "") if isinstance(qa, dict) else getattr(qa, "content", "")
            lines.append(f"Question: {q_text}")
            lines.append("")
            lines.append(f"Answer: {a_text}")
            lines.append("")
            lines.append("")
        return "\n".join(lines).strip()

    def _questions_text(self, questions):
        if not questions:
            return ""
        lines = []
        per_item_budget = max(500, self.max_section_chars // 12)
        for q in questions:
            content = self._truncate(
                self._safe_str(getattr(q, "content", "")), per_item_budget
            )
            if content:
                created_at = getattr(q, "created_at", None)
                prefix = (
                    f"[{created_at.strftime('%Y-%m-%d')}] "
                    if isinstance(created_at, datetime)
                    else ""
                )
                lines.append(f"{prefix}{content}")
        return "\n".join(lines).strip()

    def _resumes_text(self, resumes):
        if len(resumes) < 1:
            return ""
        lines = []
        for resume in resumes:
            lines.append(self._resume_text(resume))
        return "\n".join(lines).strip()

    def build_from_career_data(
        self, context: CareerData, instructions: Optional[str] = ""
    ) -> str:
        # Three top-level H1 sections. The frontend sectioned-view splits
        # on "^# " boundaries, so each H1 becomes its own card + a nav
        # entry: Resumes / Q&A / Cover Letters. Resumes-within-the-Resumes
        # section stack inside a single card (one resume = one visual
        # unit; the H3 sub-sections inside render as prose, not as
        # separate cards).
        sections = []
        if instructions:
            sections.append(instructions)

        sections.append("# Resumes\n")
        if context.resumes:
            for resume in context.resumes:
                sections.append(self._resume_text(resume))
                sections.append("")  # blank line between resumes
        else:
            sections.append("_No resumes yet._")

        # Shorter title keeps the anchor-nav label tidy (the nav pill's
        # width is bounded so labels don't jitter the cursor target).
        sections.append("# Q&A\n")
        qas = self._qas_text(context.answers)
        sections.append(qas or "_No answered questions yet._")

        sections.append("# Cover Letters\n")
        if context.cover_letters:
            for cover_letter in context.cover_letters:
                sections.append(cover_letter.content or "")
                sections.append("")
        else:
            sections.append("_No cover letters yet._")

        return "\n".join(sections).strip() + "\n"

    def build(
        self,
        context: dict,
        instructions: Optional[str] = None,
        injected_prompt: Optional[str] = None,
        career_markdown: Optional[str] = None,
    ) -> str:
        sections = []

        # --- Front matter: what is being asked ---

        # 1. User Override (caller-supplied instructions, take priority).
        # Put this BEFORE the default preamble so the model treats it as
        # the controlling directive — a trailing "Additional Instructions"
        # block loses to a strong leading system message that says e.g.
        # "OUTPUT FORMAT — strictly plain text".
        if injected_prompt:
            sections.append(
                "## User Instructions (PRIORITY — these override the default "
                "behavior and output format guidance below)\n"
                f"{injected_prompt}"
            )

        # 2. Instructions (default preamble)
        if instructions is None:
            instructions = (
                "Answer ONLY the question in the '## Question to Answer' section below. "
                "Use the provided context (job details, resume, Q&A history) to personalize "
                "your answer, but do NOT answer any questions from the Q&A History section. "
                "Be concise, truthful, and specific to the job.\n"
                "\n"
                "OUTPUT FORMAT — strictly plain text (unless the User Instructions above say otherwise):\n"
                "- No markdown, no headings (do NOT prefix with '## Answer' or any header).\n"
                "- No bullet lists, numbered lists, bold, italic, code fences, or block quotes.\n"
                "- Use plain sentences and paragraphs separated by a single blank line.\n"
                "- The user will paste your output verbatim into a job application form;\n"
                "  anything that isn't plain prose becomes literal punctuation there."
            )
        sections.append(instructions)

        # 3. Question to Answer
        question_content = self._safe_str(getattr(context["question"], "content", ""))
        if question_content:
            sections.append(f"## Question to Answer\n{question_content}")

        # --- Supporting context: background info for the model ---

        # Career Profile (pre-rendered markdown from /career-data/ endpoint)
        if career_markdown:
            sections.append(f"## Career Profile\n{career_markdown}")

        # Job Details
        job_text = self._job_post_text(context["job_post"])
        if job_text:
            sections.append(f"## Job Details\n{job_text}")

        # Company
        company_text = self._company_text(context["company"])
        if company_text:
            sections.append(f"## Company\n{company_text}")

        # Resumes
        resumes = context.get("resumes") or []
        if resumes:
            if len(resumes) == 1:
                rt = self._resume_text(resumes[0])
                if rt:
                    sections.append(f"## Resume Summary\n{rt}")
            else:
                rts = self._resumes_text(resumes)
                if rts:
                    sections.append(f"## Resumes Summary\n{rts}")
        else:
            # Fallback to single resume if provided
            single_resume = context.get("resume")
            if single_resume:
                rt = self._resume_text(single_resume)
                if rt:
                    sections.append(f"## Resume Summary\n{rt}")

        # Cover Letters
        cover_letters_text = self._cover_letters_text(context["cover_letters"])
        if cover_letters_text:
            sections.append(f"## Cover Letters\n{cover_letters_text}")

        # Q&A History (reference only — do not answer these)
        qas_text = self._qas_text(context["qas"])
        if qas_text:
            sections.append(f"## Q&A History (for reference style and tone only — do NOT answer these)\n{qas_text}")

        # --- Last word: the user's own directive, restated ---
        #
        # THE PRIORITY BLOCK AT THE TOP WAS NOT WINNING, and the arithmetic
        # says why. Doug, 2026-08-25: "I gave it a clear directive to highlight
        # mcp and it didn't." His instruction was 27 characters. Everything
        # between it and this line — career profile, job description, resumes,
        # cover letters, Q&A history — runs to tens of thousands. The word
        # PRIORITY was asserted once and then buried.
        #
        # Position is the cheapest lever available. Every section above is
        # REFERENCE MATERIAL; the directive is the TASK, and the task should be
        # the last thing read before generating. This is not a second, competing
        # instruction — it is the same string, deliberately repeated at the far
        # end, which is why it quotes rather than paraphrases.
        #
        # Only when there IS one. An empty restatement would tell the model its
        # final word is blank, the same trap `composeInjectedPrompt` avoids by
        # returning null rather than an empty string.
        if injected_prompt:
            sections.append(
                "## Reminder — the User Instructions above are the controlling "
                "directive\n"
                "Everything between them and this line is reference material. "
                "Where it conflicts with the instructions, the instructions "
                "win. Restated so they are the last thing you read:\n\n"
                f"{injected_prompt}"
            )

        return "\n\n".join(sections)
