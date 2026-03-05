from .base import BaseModel


class CareerData:
    """
    A stateless service that provides transparent access to favorite career data for users.
    Uses properties with filtering to provide dot notation access to favorites.
    """

    def __init__(self, user_id, session=None):
        self.user_id = user_id
        self._session = session

    @property
    def session(self):
        if self._session is None:
            self._session = BaseModel.get_session()
        return self._session

    # Favorite resumes - filters to only favorite=True
    @property
    def favorite_resumes(self):
        from .resume import Resume

        return (
            self.session.query(Resume)
            .filter(Resume.user_id == self.user_id, Resume.favorite == True)
            .all()
        )

    # Favorite cover letters - filters to only favorite=True
    @property
    def favorite_cover_letters(self):
        from .cover_letter import CoverLetter

        return (
            self.session.query(CoverLetter)
            .filter(CoverLetter.user_id == self.user_id, CoverLetter.favorite == True)
            .all()
        )

    # Favorite questions - filters to only favorite=True

    # Favorite answers - through favorite questions, filters to only favorite=True answers
    @property
    def favorite_answers(self):
        """Get all favorite answers from favorite questions."""
        from .answer import Answer
        from .question import Question

        return (
            self.session.query(Answer)
            .join(Question, Answer.question_id == Question.id)
            .filter(
                Question.created_by_id == self.user_id,
                Answer.favorite is True,
            )
            .all()
        )

    # Applications from favorite questions
    @property
    def question_applications(self):
        """Get all applications from favorite questions."""
        from .application import Application
        from .question import Question

        return (
            self.session.query(Application)
            .join(Question, Application.question_id == Question.id)
            .filter(Question.created_by_id == self.user_id, Question.favorite == True)
            .all()
        )

    # Shorter aliases for convenience
    @property
    def resumes(self):
        """Alias for favorite_resumes."""
        return self.favorite_resumes

    @property
    def cover_letters(self):
        """Alias for favorite_cover_letters."""
        return self.favorite_cover_letters

    @property
    def questions(self):
        """XXX Not used - Alias for favorite_questions."""
        return self.favorite_questions

    @property
    def answers(self):
        """Alias for favorite_answers."""
        return self.favorite_answers

    @property
    def applications(self):
        """XXX Not used - Alias for question_applications."""
        return self.question_applications

    @classmethod
    def for_user(cls, user_id, session=None):
        """Create a CareerData instance for a user."""
        return cls(user_id, session)

    def to_dict(self):
        """Convert the career data to a dictionary for API responses."""
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
