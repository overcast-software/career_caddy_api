from datetime import datetime

from job_hunting.models import Answer, JobApplication, JobPost, Company, CoverLetter, Question, Resume
from job_hunting.lib.services.application_prompt_builder import ApplicationPromptBuilder
from job_hunting.lib.services.prompt_utils import write_prompt_to_file


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
        self.temperature = temperature
        self.previous_limit = previous_limit  # None means no limit
        self.max_section_chars = max_section_chars
        self.prompt_builder = ApplicationPromptBuilder(
            max_section_chars=self.max_section_chars
        )
        self.question = None

    def load_context(self):
        pass

    def load_context_for_question(self, question):
        """Load context data for a given question. Can be used independently of AI generation."""
        # Reload question via Django ORM (Question is now a Django model)
        question = Question.objects.filter(pk=question.id).first()
        if not question:
            question = self.question

        # Resolve application via Django ORM
        application = None
        if getattr(question, "application_id", None):
            try:
                application = JobApplication.objects.select_related("resume").filter(
                    id=question.application_id
                ).first()
            except Exception:
                pass

        # Resolve user
        user = None
        if getattr(question, "created_by_id", None):
            try:
                from django.contrib.auth import get_user_model
                User_model = get_user_model()
                user = User_model.objects.filter(pk=question.created_by_id).first()
            except Exception:
                pass
        if not user and application and getattr(application, "user", None):
            user = application.user

        # Resolve job_post — from question directly, or via application
        job_post = None
        if getattr(question, "job_post_id", None):
            job_post = JobPost.objects.filter(pk=question.job_post_id).first()
        if not job_post and application and hasattr(application, "job_post"):
            job_post = application.job_post

        # Resolve company (via Django ORM using company_id fields)
        company = None
        if getattr(question, "company_id", None):
            company = Company.objects.filter(pk=question.company_id).first()
        elif job_post and getattr(job_post, "company_id", None):
            company = Company.objects.filter(pk=job_post.company_id).first()

        # Resolve resume(s) (favorites only)
        resumes = []
        resume = None
        if user:
            resumes = list(
                Resume.objects.filter(user_id=user.id, favorite=True).order_by("-id")
            )
        if application and getattr(application, "resume_id", None):
            try:
                resume = application.resume
            except Exception:
                resume = None
            if resume:
                # Only include application resume if it's a favorite or if no favorite resumes exist
                if getattr(resume, "favorite", False):
                    if all(r.id != resume.id for r in resumes):
                        resumes.insert(0, resume)
                elif not resumes:
                    resumes.insert(0, resume)
        else:
            resume = resumes[0] if resumes else None

        # Retrieve cover letters (favorites only)
        cover_letters = []
        if user:
            from django.db.models import Q
            cover_letters_qs = (
                CoverLetter.objects.select_related("resume")
                .filter(
                    favorite=True,
                )
                .filter(
                    Q(user_id=user.id) | Q(resume__user_id=user.id)
                )
                .order_by("-created_at", "-id")
            )
            cover_letters.extend(cover_letters_qs)

        # Add application's cover letter if not already included and it's a favorite
        if (
            application
            and hasattr(application, "cover_letter")
            and application.cover_letter
            and getattr(application.cover_letter, "favorite", False)
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

            # User-authored questions with favorited answers (excluding current)
            fav_q_ids = set(
                Answer.objects.filter(
                    question__created_by_id=user.id, favorite=True,
                ).values_list("question_id", flat=True)
            )
            user_questions = list(
                Question.objects.filter(
                    id__in=fav_q_ids,
                    created_by_id=user.id,
                ).exclude(id=question.id).order_by("created_at", "id")
            )

            question_ids.update(q.id for q in user_questions)

            # Application-linked questions with favorited answers (excluding current)
            if application:
                app_fav_q_ids = set(
                    Answer.objects.filter(
                        question__application_id=application.id, favorite=True,
                    ).values_list("question_id", flat=True)
                )
                app_questions = list(
                    Question.objects.filter(
                        id__in=app_fav_q_ids,
                        application_id=application.id,
                    ).exclude(id=question.id).order_by("created_at", "id")
                )
                question_ids.update(q.id for q in app_questions)

            # Convert to list for query
            question_ids_list = list(question_ids)

            # 2) Get answers for those questions with special logic:
            # - Include favorite answers
            # - Include non-favorite answers if the question is favorited and has only one answer
            if question_ids_list:
                # First get all favorite answers
                favorite_answers = list(
                    Answer.objects.filter(
                        question_id__in=question_ids_list,
                        favorite=True,
                    ).order_by("created_at", "id")
                )

                # Track which questions already have favorite answers
                questions_with_favorite_answers = {
                    a.question_id for a in favorite_answers
                }

                # For favorited questions without favorite answers, check if they have only one answer
                questions_without_favorite_answers = [
                    qid
                    for qid in question_ids_list
                    if qid not in questions_with_favorite_answers
                ]

                additional_answers = []
                if questions_without_favorite_answers:
                    from django.db.models import Count

                    answer_counts = (
                        Answer.objects.filter(
                            question_id__in=questions_without_favorite_answers
                        )
                        .values("question_id")
                        .annotate(count=Count("id"))
                    )

                    # Find questions with exactly one answer
                    single_answer_questions = [
                        row["question_id"] for row in answer_counts if row["count"] == 1
                    ]

                    if single_answer_questions:
                        additional_answers = list(
                            Answer.objects.filter(
                                question_id__in=single_answer_questions
                            ).order_by("created_at", "id")
                        )

                # Combine favorite answers and additional single answers
                all_answers = favorite_answers + additional_answers

                # 3) Assemble Q&A pairs
                for answer in all_answers:
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
                # Reuse app_questions from step 1 (already filtered by favorited answers)
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

    def _load_context(self):
        """Load context for the current question. Used internally by generate_answer."""
        if self.question:
            return self.load_context_for_question(self.question)
        else:
            return self.load_context()

    # Max seconds to wait for the LLM before giving up. A stuck completions
    # call would otherwise block the daemon thread forever and leave the
    # answer pending. Generous enough for long generations, strict enough
    # that "stuck forever" becomes "failed in 2 min" so the UI can react.
    _AI_CALL_TIMEOUT = 120

    def _call_ai(self, prompt: str) -> str:
        messages = [
            {
                "role": "system",
                "content": "You are an assistant helping craft job application answers. Reply in GitHub-Flavored Markdown. Be concise and professional.",
            },
            {"role": "user", "content": prompt},
        ]

        # Raise on any error so the caller (AnswerViewSet._generate) can
        # mark the answer 'failed' instead of writing empty content as
        # 'completed'. The previous `except Exception: pass` masked
        # timeouts, auth errors, and rate limits — users saw "completed"
        # answers with empty bodies OR indefinite pending states.
        if hasattr(self.ai_client, "chat") and hasattr(
            self.ai_client.chat, "completions"
        ):
            response = self.ai_client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                timeout=self._AI_CALL_TIMEOUT,
            )
            return response.choices[0].message.content or ""

        if hasattr(self.ai_client, "responses"):
            response = self.ai_client.responses.create(
                model=self.model,
                input=prompt,
                timeout=self._AI_CALL_TIMEOUT,
            )
            if hasattr(response, "output_text"):
                return response.output_text or ""
            if hasattr(response, "choices") and response.choices:
                return response.choices[0].message.content or ""
            return str(response) if response else ""

        if callable(self.ai_client):
            result = self.ai_client(prompt)
            return str(result) if result else ""

        raise RuntimeError(
            f"AnswerService: unsupported ai_client type {type(self.ai_client).__name__}"
        )

    def generate_answer(self, question: Question, save=True, injected_prompt=None, career_markdown: str = None) -> Answer:
        self.question = question
        context = self._load_context()

        # Normalize optional strings
        clean_injected = injected_prompt.strip() if isinstance(injected_prompt, str) and injected_prompt.strip() else None
        clean_career = career_markdown.strip() if isinstance(career_markdown, str) and career_markdown.strip() else None

        if clean_career:
            # Career markdown replaces context-loaded resumes
            context["resumes"] = []
            context["resume"] = None

        # Build prompt: preamble → injected instructions → question → supporting context
        prompt = self.prompt_builder.build(
            context,
            injected_prompt=clean_injected,
            career_markdown=clean_career,
        )

        write_prompt_to_file(
            prompt,
            kind="answer",
            identifiers={
                "question_id": question.id,
                "application_id": getattr(question, "application_id", None),
                "user_id": getattr(context.get("user"), "id", None),
            },
        )
        content = self._call_ai(prompt)

        answer = Answer(question_id=self.question.id, content=content)

        if save:
            answer.save()

        return answer

    def build_prompt_only(self) -> str:
        context = self._load_context()
        return self.prompt_builder.build(context)
