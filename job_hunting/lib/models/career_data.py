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

    # Favorite answers - through favorite questions
    @property
    def favorite_answers(self):
        from job_hunting.models.answer import Answer
        from job_hunting.models.question import Question

        question_ids = list(
            Question.objects.filter(
                created_by_id=self.user_id, favorite=True
            ).values_list("id", flat=True)
        )
        return list(Answer.objects.filter(question_id__in=question_ids, favorite=True))

    # Applications linked to favorite questions
    @property
    def question_applications(self):
        from job_hunting.models.job_application import JobApplication
        from job_hunting.models.question import Question

        app_ids = list(
            Question.objects.filter(
                created_by_id=self.user_id,
                favorite=True,
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
