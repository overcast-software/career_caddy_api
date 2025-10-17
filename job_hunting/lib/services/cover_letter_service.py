from jinja2 import Environment, FileSystemLoader
from job_hunting.lib.models.cover_letter import CoverLetter
from job_hunting.lib.services.db_export_service import DbExportService


class CoverLetterService:
    def __init__(self, ai_client, job_post, resume):
        self.job_post = job_post
        self.resume = resume
        self.ai_client = ai_client

    def generate_cover_letter(self):
        env = Environment(loader=FileSystemLoader("templates"))
        tmpl = env.get_template("cover_letter_prompt.j2")
        exporter = DbExportService()
        resume_markdown = exporter.resume_markdown_export(self.resume)
        prompt = tmpl.render(
            job_title=self.job_post.title,
            company_name=getattr(self.job_post.company, "name", ""),
            job_description=self.job_post.description,
            resume=resume_markdown,
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

        cover_letter, created = CoverLetter.first_or_create(
            content=cover_letter_content,
            user_id=self.resume.user.id,
            resume_id=self.resume.id,
            job_post_id=self.job_post.id,
        )
        return cover_letter
