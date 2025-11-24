from typing import Optional, List, Dict
from datetime import datetime
from sqlalchemy import desc
from sqlalchemy.orm import joinedload, selectinload

from job_hunting.lib.models import (
    Answer, Question, Application, JobPost, Company, Resume, User, BaseModel
)


class AnswerService:
    def __init__(
        self,
        ai_client,
        question: Question,
        session=None,
        model="gpt-4o-mini",
        temperature=0.3,
        previous_limit=10,
        max_section_chars=6000
    ):
        self.ai_client = ai_client
        self.question = question
        self.session = session
        self.model = model
        self.temperature = temperature
        self.previous_limit = previous_limit
        self.max_section_chars = max_section_chars

    def _get_session(self):
        return self.session or BaseModel.get_session()

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
        session = self._get_session()
        
        # Reload question with relationships
        question = session.query(Question).options(
            joinedload(Question.application).joinedload(Application.user),
            joinedload(Question.application).joinedload(Application.job_post).joinedload(JobPost.company),
            joinedload(Question.application).joinedload(Application.resume),
            joinedload(Question.user),
            joinedload(Question.company)
        ).filter_by(id=self.question.id).first()
        
        if not question:
            question = self.question

        # Resolve user
        user = None
        if hasattr(question, 'user') and question.user:
            user = question.user
        elif hasattr(question, 'application') and question.application and question.application.user:
            user = question.application.user

        # Resolve application
        application = getattr(question, 'application', None)

        # Resolve job_post
        job_post = None
        if application and hasattr(application, 'job_post'):
            job_post = application.job_post

        # Resolve company
        company = None
        if hasattr(question, 'company') and question.company:
            company = question.company
        elif job_post and hasattr(job_post, 'company'):
            company = job_post.company

        # Resolve resume
        resume = None
        if application and hasattr(application, 'resume'):
            resume = application.resume
        elif user:
            resume = session.query(Resume).filter_by(user_id=user.id).order_by(desc(Resume.id)).first()

        # Retrieve previous Q&A
        previous_qas = []
        if user:
            # Query answers joined to questions by the same user, excluding current question
            answers_query = session.query(Answer).join(Question).filter(
                Question.id != question.id
            ).filter(
                (Question.created_by_id == user.id) |
                (Question.application.has(user_id=user.id))
            ).order_by(desc(Answer.created_at), desc(Answer.id)).limit(self.previous_limit)
            
            for answer in answers_query:
                previous_qas.append({
                    "question": getattr(answer.question, 'content', ''),
                    "answer": getattr(answer, 'content', ''),
                    "asked_at": getattr(answer, 'created_at', None)
                })

        return {
            'user': user,
            'application': application,
            'job_post': job_post,
            'company': company,
            'resume': resume,
            'previous_qas': previous_qas,
            'question': question
        }

    def _resume_text(self, resume):
        if not resume:
            return ""
        
        try:
            if hasattr(resume, 'collated_content'):
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
            header = context.get('header', {})
            if header.get('name'):
                parts.append(f"Name: {header['name']}")
            if header.get('title'):
                parts.append(f"Title: {header['title']}")
            
            # Summary
            summary = context.get('summary', '').strip()
            if summary:
                parts.append(f"Summary: {summary}")
            
            # Experiences (limit to 3-5 bullet points each)
            experiences = context.get('experiences', [])
            if experiences:
                exp_parts = []
                for exp in experiences[:5]:  # Limit experiences
                    exp_text = f"{exp.get('title', '')} at {exp.get('company', '')}"
                    if exp.get('date_range'):
                        exp_text += f" ({exp['date_range']})"
                    descriptions = exp.get('descriptions', [])[:3]  # Limit bullets
                    if descriptions:
                        exp_text += "\n" + "\n".join(f"- {desc}" for desc in descriptions)
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
        
        title = self._safe_str(getattr(job_post, 'title', ''))
        if title:
            parts.append(f"Title: {title}")
        
        if hasattr(job_post, 'company') and job_post.company:
            company_name = self._safe_str(getattr(job_post.company, 'name', ''))
            if company_name:
                parts.append(f"Company: {company_name}")
        
        posted_date = getattr(job_post, 'posted_date', None)
        if posted_date:
            parts.append(f"Posted: {posted_date}")
        
        link = self._safe_str(getattr(job_post, 'link', ''))
        if link:
            parts.append(f"Link: {link}")
        
        description = self._safe_str(getattr(job_post, 'description', ''))
        if description:
            parts.append(f"Description: {self._truncate(description, self.max_section_chars // 2)}")
        
        return "\n".join(parts)

    def _company_text(self, company):
        if not company:
            return ""
        
        parts = []
        name = self._safe_str(getattr(company, 'name', ''))
        if name:
            parts.append(f"Name: {name}")
        
        display_name = self._safe_str(getattr(company, 'display_name', ''))
        if display_name and display_name != name:
            parts.append(f"Display Name: {display_name}")
        
        return "\n".join(parts)

    def _previous_qas_text(self, previous_qas):
        if not previous_qas:
            return ""
        
        lines = []
        per_item_budget = max(100, self.max_section_chars // 10)
        
        for qa in previous_qas:
            question = self._truncate(qa.get('question', ''), per_item_budget)
            answer = self._truncate(qa.get('answer', ''), per_item_budget)
            if question and answer:
                lines.append(f"Q: {question}")
                lines.append(f"A: {answer}")
                lines.append("")  # Empty line between Q&A pairs
        
        return "\n".join(lines).strip()

    def build_prompt(self, context) -> str:
        sections = []
        
        # Instructions
        sections.append(
            "Answer the user's application question in clear, professional markdown. "
            "Be concise, truthful, and specific to the job. If you need to assume, state assumptions briefly."
        )
        
        # Current Question
        question_content = self._safe_str(getattr(context['question'], 'content', ''))
        if question_content:
            sections.append(f"## Current Question\n{question_content}")
        
        # Job Details
        job_text = self._job_post_text(context['job_post'])
        if job_text:
            sections.append(f"## Job Details\n{job_text}")
        
        # Company
        company_text = self._company_text(context['company'])
        if company_text:
            sections.append(f"## Company\n{company_text}")
        
        # Resume Summary
        resume_text = self._resume_text(context['resume'])
        if resume_text:
            sections.append(f"## Resume Summary\n{resume_text}")
        
        # Previous Q&A
        previous_text = self._previous_qas_text(context['previous_qas'])
        if previous_text:
            sections.append(f"## Previous Q&A\n{previous_text}")
        
        return "\n\n".join(sections)

    def _call_ai(self, prompt: str) -> str:
        messages = [
            {
                "role": "system",
                "content": "You are an assistant helping craft job application answers. Reply in GitHub-Flavored Markdown. Be concise and professional."
            },
            {
                "role": "user",
                "content": prompt
            }
        ]
        
        try:
            # Try OpenAI chat completions API
            if hasattr(self.ai_client, "chat") and hasattr(self.ai_client.chat, "completions"):
                response = self.ai_client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature
                )
                return response.choices[0].message.content or ""
            
            # Try responses API
            elif hasattr(self.ai_client, "responses"):
                response = self.ai_client.responses.create(
                    model=self.model,
                    input=prompt
                )
                # Handle different response formats
                if hasattr(response, 'output_text'):
                    return response.output_text or ""
                elif hasattr(response, 'choices') and response.choices:
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

    def generate_answer(self, save=True) -> Answer:
        context = self._load_context()
        prompt = self.build_prompt(context)
        content = self._call_ai(prompt)
        
        answer = Answer(
            question_id=self.question.id,
            content=content
        )
        
        if save:
            answer.save()
        
        return answer

    def build_prompt_only(self) -> str:
        context = self._load_context()
        return self.build_prompt(context)
