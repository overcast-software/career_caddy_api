class CareerData:
    """
    A stateless service that provides transparent access to favorite career data for users.
    Uses properties with filtering to provide dot notation access to favorites.
    """

    def __init__(self, user_id, session=None):
        self.user_id = user_id

    # Favorite resumes - filters to only favorite=True
    @property
    def favorite_resumes(self):
        from job_hunting.models.resume import Resume

        return list(Resume.objects.filter(user_id=self.user_id, favorite=True))

    # Favorite cover letters - filters to only favorite=True
    @property
    def favorite_cover_letters(self):
        from job_hunting.models.cover_letter import CoverLetter

        return list(CoverLetter.objects.filter(user_id=self.user_id, favorite=True))

    # Favorite answers - directly by answer.favorite
    @property
    def favorite_answers(self):
        from job_hunting.models.answer import Answer

        return list(
            Answer.objects.filter(
                question__created_by_id=self.user_id, favorite=True
            )
        )

    # Applications linked to questions that have favorited answers
    @property
    def question_applications(self):
        from job_hunting.models.answer import Answer
        from job_hunting.models.job_application import JobApplication
        from job_hunting.models.question import Question

        question_ids = Answer.objects.filter(
            question__created_by_id=self.user_id, favorite=True,
        ).values_list("question_id", flat=True).distinct()

        app_ids = list(
            Question.objects.filter(
                pk__in=question_ids,
                application_id__isnull=False,
            ).values_list("application_id", flat=True).distinct()
        )
        return list(JobApplication.objects.filter(pk__in=app_ids))

    # Shorter aliases for convenience
    @property
    def resumes(self):
        return self.favorite_resumes

    @property
    def cover_letters(self):
        return self.favorite_cover_letters

    @property
    def answers(self):
        return self.favorite_answers

    @property
    def applications(self):
        return self.question_applications

    @classmethod
    def for_user(cls, user_id, session=None):
        return cls(user_id)

    def to_refs(self):
        return {
            "resume_ids": [r.id for r in self.favorite_resumes],
            "cover_letter_ids": [cl.id for cl in self.favorite_cover_letters],
            "answer_ids": [a.id for a in self.favorite_answers],
        }

    def to_sections(self):
        """Structured per-item view for the frontend.

        Companion to build_from_career_data (which emits one big markdown
        blob for prompting + copy-to-clipboard). This returns the same
        content sliced into typed items so the UI can render one card per
        resume / Q&A / cover letter and the anchor-nav can label each
        with a positional index like 'Resume #1'.
        """
        from job_hunting.lib.services.application_prompt_builder import (
            ApplicationPromptBuilder,
        )

        builder = ApplicationPromptBuilder()

        resume_items = []
        for r in self.favorite_resumes:
            user_name = ""
            if r.user:
                user_name = r.user.get_full_name() or r.user.username or ""
            resume_items.append(
                {
                    "id": r.id,
                    # Card + nav label use the resume's own title (what the
                    # user called it — "Senior Platform Eng — 2025 Q2",
                    # etc.). Their name is metadata on the second line.
                    "title": r.title or user_name or f"Resume {r.id}",
                    "subtitle": user_name,
                    "markdown": builder._resume_text(r),
                }
            )

        qa_items = [
            {
                "id": a.id,
                "question_id": a.question_id,
                "question": getattr(
                    getattr(a, "question", None), "content", ""
                )
                or "",
                "answer": a.content or "",
            }
            for a in self.favorite_answers
        ]

        cover_letter_items = []
        for cl in self.favorite_cover_letters:
            job_title = ""
            company_name = ""
            if getattr(cl, "job_post", None):
                job_title = cl.job_post.title or ""
                if getattr(cl.job_post, "company", None):
                    company_name = cl.job_post.company.name or ""
            created_at = getattr(cl, "created_at", None)
            cover_letter_items.append(
                {
                    "id": cl.id,
                    "job": job_title,
                    "company": company_name,
                    "created_at": created_at.isoformat() if created_at else None,
                    "content": cl.content or "",
                }
            )

        return [
            {"type": "resumes", "title": "Resumes", "items": resume_items},
            {"type": "qas", "title": "Q&A", "items": qa_items},
            {
                "type": "cover_letters",
                "title": "Cover Letters",
                "items": cover_letter_items,
            },
        ]

    def to_dict(self):
        return {
            "user_id": self.user_id,
            "favorite_resumes": {
                "count": len(self.favorite_resumes),
                "items": [
                    {"id": r.id, "title": r.title} for r in self.favorite_resumes
                ],
            },
            "favorite_cover_letters": {
                "count": len(self.favorite_cover_letters),
                "items": [{"id": cl.id} for cl in self.favorite_cover_letters],
            },
            "favorite_answers": {
                "count": len(self.favorite_answers),
                "items": [
                    {"id": a.id, "question_id": a.question_id}
                    for a in self.favorite_answers
                ],
            },
        }
