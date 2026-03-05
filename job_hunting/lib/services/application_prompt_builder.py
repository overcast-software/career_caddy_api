from typing import Optional
from datetime import datetime
from job_hunting.lib.models import CareerData
from jinja2 import Environment, FileSystemLoader


class ApplicationPromptBuilder:
    """
    ApplicationPromptBuilder instantiates a career-data
    and ports it to markdown
    build: generic build
    build_from_career_data uses career-data
    """

    def __init__(self, max_section_chars=60000):
        self.max_section_chars = max_section_chars

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
        env = Environment(loader=FileSystemLoader("templates"))
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

    def _qas_text(self, answer):
        if not answer:
            return ""
        lines = []
        lines.append(f"Q: {answer.question.content}")
        lines.append(f"A: {answer.content}")
        lines.append("")  # Empty line between Q&A pairs
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
        sections = []
        sections.append(instructions)
        sections.append("#Resumes")
        for resume in context.resumes:
            sections.append(self._resume_text(resume))

        sections.append("#Prior Questions and Answers")
        for answer in context.answers:
            sections.append(self._qas_text(answer))

        sections.append("#Coverletters")
        for cover_letter in context.cover_letters:
            sections.append(cover_letter.content)
            sections.append("")
        return "\n".join(sections)

    def build(self, context: dict, instructions: Optional[str] = None) -> str:
        sections = []

        # Instructions
        if instructions is None:
            instructions = (
                "Answer the user's application question in clear, professional markdown. "
                "Be concise, truthful, and specific to the job. If you need to assume, state assumptions briefly. "
                "Use the provided contextual information to make a more personalized answer."
            )
        sections.append(instructions)

        # Current Question
        question_content = self._safe_str(getattr(context["question"], "content", ""))
        if question_content:
            sections.append(f"## Current Question\n{question_content}")

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
            rt = self._resume_text(context.get("resume"))
            if rt:
                sections.append(f"## Resume Summary\n{rt}")

        # Cover Letters
        cover_letters_text = self._cover_letters_text(context["cover_letters"])
        if cover_letters_text:
            sections.append(f"## Cover Letters\n{cover_letters_text}")

        # Q&A History
        qas_text = self._qas_text(context["qas"])
        if qas_text:
            sections.append(f"## Q&A History\n{qas_text}")

        return "\n\n".join(sections)
