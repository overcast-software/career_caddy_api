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
        self.session = self._get_session()
        self.temperature = temperature
        self.previous_limit = previous_limit  # None means no limit
        self.max_section_chars = max_section_chars
        self.prompt_builder = ApplicationPromptBuilder(max_section_chars=self.max_section_chars)
        self.question = None

    def _get_session(self):
        return BaseModel.get_session()


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

        # Resolve resume(s) (favorites only)
        resumes = []
        resume = None
        if user:
            resumes = (
                self.session.query(Resume)
                .filter_by(user_id=user.id, favorite=True)
                .order_by(desc(Resume.id))
                .all()
            )
        if application and hasattr(application, "resume") and application.resume:
            resume = application.resume
            # Only include application resume if it's a favorite or if no favorite resumes exist
            if resume and getattr(resume, "favorite", False):
                if all(r.id != getattr(resume, "id", None) for r in resumes):
                    resumes.insert(0, resume)
            elif not resumes:  # Fallback if no favorite resumes exist
                resumes.insert(0, resume)
        else:
            resume = resumes[0] if resumes else None

        # Retrieve cover letters (favorites only)
        cover_letters = []
        if user:
            # Get favorite cover letters for the user (owned by user OR associated to user's resumes)
            cover_letters_query = (
                self.session.query(CoverLetter)
                .options(
                    joinedload(CoverLetter.job_post).joinedload(JobPost.company),
                    joinedload(CoverLetter.resume),
                )
                .filter(
                    CoverLetter.favorite == True,
                    or_(
                        CoverLetter.user_id == user.id,
                        CoverLetter.resume.has(user_id=user.id),
                    )
                )
                .order_by(desc(CoverLetter.created_at), desc(CoverLetter.id))
            )
            cover_letters.extend(cover_letters_query.all())

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

            # User-authored favorite questions (excluding current)
            user_questions = (
                self.session.query(Question)
                .filter(
                    Question.created_by_id == user.id, 
                    Question.id != question.id,
                    Question.favorite == True
                )
                .order_by(Question.created_at, Question.id)
                .all()
            )
            question_ids.update(q.id for q in user_questions)

            # Application-linked favorite questions (excluding current)
            if application:
                app_questions = (
                    self.session.query(Question)
                    .filter(
                        Question.application_id == application.id,
                        Question.id != question.id,
                        Question.favorite == True,
                    )
                    .order_by(Question.created_at, Question.id)
                    .all()
                )
                question_ids.update(q.id for q in app_questions)

            # Convert to list for query
            question_ids_list = list(question_ids)

            # 2) Get answers for those questions with special logic:
            # - Include favorite answers
            # - Include non-favorite answers if the question is favorited and has only one answer
            if question_ids_list:
                # First get all favorite answers
                favorite_answers = (
                    self.session.query(Answer)
                    .options(joinedload(Answer.question))
                    .filter(
                        Answer.question_id.in_(question_ids_list),
                        Answer.favorite == True
                    )
                    .order_by(Answer.created_at, Answer.id)
                    .all()
                )

                # Track which questions already have favorite answers
                questions_with_favorite_answers = {a.question_id for a in favorite_answers}
                
                # For favorited questions without favorite answers, check if they have only one answer
                questions_without_favorite_answers = [
                    qid for qid in question_ids_list 
                    if qid not in questions_with_favorite_answers
                ]
                
                additional_answers = []
                if questions_without_favorite_answers:
                    # Get count of answers per question for questions without favorite answers
                    from sqlalchemy import func
                    answer_counts = (
                        self.session.query(Answer.question_id, func.count(Answer.id))
                        .filter(Answer.question_id.in_(questions_without_favorite_answers))
                        .group_by(Answer.question_id)
                        .all()
                    )
                    
                    # Find questions with exactly one answer
                    single_answer_questions = [
                        qid for qid, count in answer_counts if count == 1
                    ]
                    
                    if single_answer_questions:
                        # Get the single answers for these questions
                        additional_answers = (
                            self.session.query(Answer)
                            .options(joinedload(Answer.question))
                            .filter(Answer.question_id.in_(single_answer_questions))
                            .order_by(Answer.created_at, Answer.id)
                            .all()
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

            # 4) Build unified favorite questions list (user-authored + application-linked), de-duped
            all_questions = list(user_questions)
            if application:
                app_questions = (
                    self.session.query(Question)
                    .filter(
                        Question.application_id == application.id,
                        Question.id != question.id,
                        Question.favorite == True,
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
        prompt = self.prompt_builder.build(context)

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
