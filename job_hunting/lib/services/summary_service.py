from job_hunting.lib.models import JobPost, Resume, Summary
from jinja2 import Environment, FileSystemLoader
from .db_export_service import DbExportService


class SummaryService:

    def __init__(self, ai_client, job: JobPost, resume: Resume):
        self.job = job
        self.resume = resume
        self.env = Environment(loader=FileSystemLoader("templates"))
        self.ai_client = ai_client

    def generate_summary(self) -> Summary:
        exporter = DbExportService()
        resume_markdown = exporter.resume_markdown_export(self.resume)

        template = self.env.get_template("summary_service_prompt.j2")

        prompt = template.render(
            job_description=self.job.description, resume=resume_markdown
        )

        write_prompt_to_file(
            prompt,
            kind="summary",
            identifiers={
                "job_post_id": self.job.id,
                "resume_id": self.resume.id,
                "user_id": self.resume.user_id,
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
        content = response.choices[0].message.content.strip()
        new_summary = Summary(
            job_post_id=self.job.id,
            user_id=self.resume.user_id,
            content=content,
        )
        new_summary.save()
        return new_summary
