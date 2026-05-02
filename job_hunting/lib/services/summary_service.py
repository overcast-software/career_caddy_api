from job_hunting.models import Resume
from job_hunting.models import JobPost
from job_hunting.models import Summary
from jinja2 import Environment, FileSystemLoader
from .db_export_service import DbExportService
from job_hunting.lib.services.prompt_utils import write_prompt_to_file


class SummaryService:

    def __init__(self, ai_client, job: JobPost, resume: Resume = None, resume_markdown: str = None, user_id: int = None):
        self.job = job
        self.resume = resume
        self._resume_markdown = resume_markdown
        self._user_id = user_id
        self.env = Environment(loader=FileSystemLoader("templates"), autoescape=False)  # nosec B701 - text/LLM prompt templates, not HTML
        self.ai_client = ai_client

    def generate_content(self, injected_prompt=None) -> str:
        if self._resume_markdown is not None:
            resume_markdown = self._resume_markdown
        else:
            exporter = DbExportService()
            resume_markdown = exporter.resume_markdown_export(self.resume)

        user_id = self._user_id or (self.resume.user_id if self.resume else None)
        resume_id = self.resume.id if self.resume else None

        template = self.env.get_template("summary_service_prompt.j2")

        prompt = template.render(
            job_description=self.job.description,
            resume=resume_markdown,
            injected_prompt=injected_prompt,
        )

        write_prompt_to_file(
            prompt,
            kind="summary",
            identifiers={
                "job_post_id": self.job.id,
                "resume_id": resume_id,
                "user_id": user_id,
            },
        )

        response = self.ai_client.chat.completions.create(
            model="gpt-5",
            messages=[
                {
                    "role": "system",
                    "content": "You are a career counselor",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            # max_tokens=150 not supported with gpt-5
        )
        return response.choices[0].message.content.strip()

    def generate_summary(self, injected_prompt=None) -> Summary:
        user_id = self._user_id or (self.resume.user_id if self.resume else None)
        content = self.generate_content(injected_prompt)
        return Summary.objects.create(
            job_post_id=self.job.id,
            user_id=user_id,
            content=content,
        )
