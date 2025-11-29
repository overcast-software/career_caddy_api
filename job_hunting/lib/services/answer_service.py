from typing import Optional, List, Dict
from datetime import datetime
from sqlalchemy import desc, or_
from sqlalchemy.orm import joinedload, selectinload

from job_hunting.lib.models import (
    Answer,
    Question,
    Application,
    JobPost,
    Company,
    Resume,
    User,
    CoverLetter,
    BaseModel,
)


class AnswerService:
    def __init__(
        self,
        ai_client,
        model="gpt-4o-mini",
        temperature=0.3,
        previous_limit=None,
        max_section_chars=60000,
    ):
        self.ai_client = ai_client
        self.model = model
        self.session = self._get_session()
        self.temperature = temperature
        self.previous_limit = previous_limit  # None means no limit
        self.max_section_chars = max_section_chars
        self.question = None

    def _get_session(self):
        return BaseModel.get_session()

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

    def _load_context(self):

        # Reload question with relationships
        question = (
            self.session.query(Question)
            .options(
                joinedload(Question.application).joinedload(Application.user),
                joinedload(Question.application)
                .joinedload(Application.job_post)
                .joinedload(JobPost.company),
                joinedload(Question.application).joinedload(Application.resume),
                joinedload(Question.user),
                joinedload(Question.company),
            )
            .filter_by(id=self.question.id)
            .first()
        )

        self.question = question
        if not question:
            question = self.question

        # Resolve user
        user = None
        if hasattr(question, "user") and question.user:
            user = question.user
        elif (
            hasattr(question, "application")
            and question.application
            and question.application.user
        ):
            user = question.application.user

        # Resolve application
        application = getattr(question, "application", None)

        # Resolve job_post
        job_post = None
        if application and hasattr(application, "job_post"):
            job_post = application.job_post

        # Resolve company
        company = None
        if hasattr(question, "company") and question.company:
            company = question.company
        elif job_post and hasattr(job_post, "company"):
            company = job_post.company

        # Resolve resume(s)
        resumes = []
        resume = None
        if user:
            resumes = (
                self.session.query(Resume)
                .filter_by(user_id=user.id)
                .order_by(desc(Resume.id))
                .all()
            )
        if application and hasattr(application, "resume") and application.resume:
            resume = application.resume
            if resume and all(r.id != getattr(resume, "id", None) for r in resumes):
                resumes.insert(0, resume)
        else:
            resume = resumes[0] if resumes else None

        # Retrieve cover letters
        cover_letters = []
        if user:
            # Get all cover letters for the user (owned by user OR associated to user's resumes)
            cover_letters_query = (
                self.session.query(CoverLetter)
                .options(
                    joinedload(CoverLetter.job_post).joinedload(JobPost.company),
                    joinedload(CoverLetter.resume),
                )
                .filter(
                    or_(
                        CoverLetter.user_id == user.id,
                        CoverLetter.resume.has(user_id=user.id),
                    )
                )
                .order_by(desc(CoverLetter.created_at), desc(CoverLetter.id))
            )
            cover_letters.extend(cover_letters_query.all())

        # Add application's cover letter if not already included
        if (
            application
            and hasattr(application, "cover_letter")
            and application.cover_letter
        ):
            app_cover_letter = application.cover_letter
            if not any(cl.id == app_cover_letter.id for cl in cover_letters):
                cover_letters.append(app_cover_letter)

        # Retrieve full Q&A history
        qas = []
        questions = []
        if user:
            # 1) Build unified set of question IDs from multiple sources
            question_ids = set()

            # User-authored questions (excluding current)
            breakpoint()
            user_questions = (
                self.session.query(Question)
                .filter(Question.created_by_id == user.id, Question.id != question.id)
                .order_by(Question.created_at, Question.id)
                .all()
            )
            question_ids.update(q.id for q in user_questions)

            # Application-linked questions (excluding current)
            if application:
                app_questions = (
                    self.session.query(Question)
                    .filter(
                        Question.application_id == application.id,
                        Question.id != question.id,
                    )
                    .order_by(Question.created_at, Question.id)
                    .all()
                )
                question_ids.update(q.id for q in app_questions)

            # Convert to list for query
            question_ids_list = list(question_ids)

            # 2) Get answers for those questions (no limit)
            if question_ids_list:
                answers_query = (
                    self.session.query(Answer)
                    .options(joinedload(Answer.question))
                    .filter(Answer.question_id.in_(question_ids_list))
                    .order_by(Answer.created_at, Answer.id)
                    .all()
                )

                # 3) Assemble Q&A pairs
                for answer in answers_query:
                    qas.append(
                        {
                            "question": getattr(answer.question, "content", ""),
                            "answer": getattr(answer, "content", ""),
                            "asked_at": getattr(answer.question, "created_at", None),
                            "answered_at": getattr(answer, "created_at", None),
                            "question_id": getattr(answer.question, "id", None),
                            "answer_id": getattr(answer, "id", None),
                        }
                    )

            # 4) Build unified questions list (user-authored + application-linked), de-duped
            all_questions = list(user_questions)
            if application:
                app_questions = (
                    self.session.query(Question)
                    .filter(
                        Question.application_id == application.id,
                        Question.id != question.id,
                    )
                    .order_by(Question.created_at, Question.id)
                    .all()
                )
                # Add app questions that aren't already in the list
                existing_ids = {q.id for q in all_questions}
                for app_q in app_questions:
                    if app_q.id not in existing_ids:
                        all_questions.append(app_q)

            # Sort by created_at, then id
            questions = sorted(
                all_questions, key=lambda q: (q.created_at or datetime.min, q.id)
            )

        return {
            "user": user,
            "application": application,
            "job_post": job_post,
            "company": company,
            "resume": resume,
            "resumes": resumes,
            "cover_letters": cover_letters,
            "questions": questions,
            "qas": qas,
            "previous_qas": qas,  # Backward compatibility
            "question": question,
        }

    def _resume_text(self, resume):
        if not resume:
            return ""

        try:
            if hasattr(resume, "collated_content"):
                content = resume.collated_content()
                if content:
                    return self._truncate(content, self.max_section_chars)
        except Exception:
            pass

        try:
            # Fallback to export context
            context = resume.to_export_context()
            parts = []

            # Header
            header = context.get("header", {})
            if header.get("name"):
                parts.append(f"Name: {header['name']}")
            if header.get("title"):
                parts.append(f"Title: {header['title']}")

            # Summary
            summary = context.get("summary", "").strip()
            if summary:
                parts.append(f"Summary: {summary}")

            # Experiences (limit to 3-5 bullet points each)
            experiences = context.get("experiences", [])
            if experiences:
                exp_parts = []
                for exp in experiences[:5]:  # Limit experiences
                    exp_text = f"{exp.get('title', '')} at {exp.get('company', '')}"
                    if exp.get("date_range"):
                        exp_text += f" ({exp['date_range']})"
                    descriptions = exp.get("descriptions", [])[:3]  # Limit bullets
                    if descriptions:
                        exp_text += "\n" + "\n".join(
                            f"- {desc}" for desc in descriptions
                        )
                    exp_parts.append(exp_text)
                parts.append("Experience:\n" + "\n\n".join(exp_parts))

            content = "\n\n".join(parts)
            return self._truncate(content, self.max_section_chars)
        except Exception:
            return ""

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
        per_item_budget = max(1000, self.max_section_chars // 8)

        for qa in qas:
            question = self._truncate(qa.get("question", ""), per_item_budget)
            answer = self._truncate(qa.get("answer", ""), per_item_budget)
            if question and answer:
                lines.append(f"Q: {question}")
                lines.append(f"A: {answer}")
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
        if not resumes:
            return ""
        pieces = []
        per_item_budget = max(3000, self.max_section_chars // max(1, len(resumes)))
        for r in resumes:
            header_bits = []
            name = self._safe_str(getattr(r, "name", ""))
            if name:
                header_bits.append(name)
            created_at = getattr(r, "created_at", None)
            if isinstance(created_at, datetime):
                header_bits.append(created_at.strftime("%Y-%m-%d"))
            header = " | ".join(header_bits) if header_bits else "Resume"
            text = self._resume_text(r)
            text = self._truncate(text, per_item_budget)
            if text:
                pieces.append(f"{header}\n{text}")
        return "\n\n".join(pieces).strip()

    def build_prompt(self, context) -> str:
        sections = []

        # Instructions
        sections.append(
            "Answer the user's application question in clear, professional markdown. "
            "Be concise, truthful, and specific to the job. If you need to assume, state assumptions briefly."
            "User the provided contextual information to make a more personalized answer."
        )

        # Current Question
        question_content = self._safe_str(getattr(context["question"], "content", ""))
        if question_content:
            sections.append(f"## Current Question\n{question_content}")

        breakpoint()
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

        # User Questions
        questions_text = self._questions_text(context.get("questions", []))
        if questions_text:
            sections.append(f"## User Questions\n{questions_text}")

        # Q&A History
        qas_text = self._qas_text(context["qas"])
        if qas_text:
            sections.append(f"## Q&A History\n{qas_text}")

        return "\n\n".join(sections)

    def _call_ai(self, prompt: str) -> str:
        messages = [
            {
                "role": "system",
                "content": "You are an assistant helping craft job application answers. Reply in GitHub-Flavored Markdown. Be concise and professional.",
            },
            {"role": "user", "content": prompt},
        ]

        try:
            # Try OpenAI chat completions API
            if hasattr(self.ai_client, "chat") and hasattr(
                self.ai_client.chat, "completions"
            ):
                response = self.ai_client.chat.completions.create(
                    model=self.model, messages=messages, temperature=self.temperature
                )
                return response.choices[0].message.content or ""

            # Try responses API
            elif hasattr(self.ai_client, "responses"):
                response = self.ai_client.responses.create(
                    model=self.model, input=prompt
                )
                # Handle different response formats
                if hasattr(response, "output_text"):
                    return response.output_text or ""
                elif hasattr(response, "choices") and response.choices:
                    return response.choices[0].message.content or ""
                else:
                    return str(response) if response else ""

            # Fallback: callable client
            elif callable(self.ai_client):
                result = self.ai_client(prompt)
                return str(result) if result else ""

        except Exception:
            pass

        return ""

    def generate_answer(self, question: Question, save=True) -> Answer:
        self.question = question
        context = self._load_context()
        prompt = self.build_prompt(context)

        with open("prompt.txt", "w") as file:
            print("*" * 88)
            print("write")
            print("*" * 88)
            file.write(prompt)
        content = self._call_ai(prompt)

        answer = Answer(question_id=self.question.id, content=content)

        if save:
            answer.save()

        return answer

    def build_prompt_only(self) -> str:
        context = self._load_context()
        return self.build_prompt(context)
