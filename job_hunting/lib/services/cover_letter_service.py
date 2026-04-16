from jinja2 import Environment, FileSystemLoader
from job_hunting.models import CoverLetter
from job_hunting.lib.services.db_export_service import DbExportService
from job_hunting.lib.services.prompt_utils import write_prompt_to_file


class CoverLetterService:
    def __init__(self, ai_client, job_post, resume=None, resume_markdown=None, user_id=None):
        self.job_post = job_post
        self.resume = resume
        self.ai_client = ai_client
        self._resume_markdown = resume_markdown
        self._user_id = user_id

    def generate_cover_letter(self, injected_prompt=None):
        env = Environment(loader=FileSystemLoader("templates"), autoescape=False)  # nosec B701 - text/LLM prompt templates, not HTML
        tmpl = env.get_template("cover_letter_prompt.j2")

        if self._resume_markdown is not None:
            resume_markdown = self._resume_markdown
        else:
            exporter = DbExportService()
            resume_markdown = exporter.resume_markdown_export(self.resume)

        prompt = tmpl.render(
            job_title=self.job_post.title,
            company_name=getattr(self.job_post.company, "name", ""),
            job_description=self.job_post.description,
            resume=resume_markdown,
            injected_prompt=injected_prompt,
        )

        user_id = self._user_id or (self.resume.user.id if self.resume else None)
        resume_id = self.resume.id if self.resume else None

        write_prompt_to_file(
            prompt,
            kind="cover_letter",
            identifiers={
                "job_post_id": self.job_post.id,
                "resume_id": resume_id,
                "user_id": user_id,
            },
        )

        completion = self.ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "You are a professional cover letter writer. Output only the letter text.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
        )
        cover_letter_content = completion.choices[0].message.content.strip()

        cover_letter, created = CoverLetter.objects.get_or_create(
            content=cover_letter_content,
            user_id=user_id,
            resume_id=resume_id,
            job_post_id=self.job_post.id,
        )
        return cover_letter
