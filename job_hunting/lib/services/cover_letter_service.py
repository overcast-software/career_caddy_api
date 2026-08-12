from jinja2 import Environment, FileSystemLoader
from job_hunting.lib.ai_client import (
    is_temperature_error,
    note_temperature_rejected,
    rejects_temperature,
    resolve_model,
)
from job_hunting.lib.services.db_export_service import DbExportService
from job_hunting.lib.services.prompt_utils import write_prompt_to_file

# CC: like answers, cover letters generate prose the user actually sends, and
# were likewise pinned to the cheapest model with no env override and no entry
# in the admin agent-role registry. Same convention as every other role now:
# COVER_LETTER_MODEL -> CADDY_DEFAULT_MODEL -> default.
COVER_LETTER_MODEL_ENV = "COVER_LETTER_MODEL"
COVER_LETTER_MODEL_DEFAULT = "openai:gpt-5"


class CoverLetterService:
    def __init__(
        self,
        ai_client,
        job_post,
        resume=None,
        resume_markdown=None,
        user_id=None,
        model=None,
    ):
        self.job_post = job_post
        self.resume = resume
        self.ai_client = ai_client
        self._resume_markdown = resume_markdown
        self._user_id = user_id
        self.model = model or resolve_model(
            COVER_LETTER_MODEL_ENV, COVER_LETTER_MODEL_DEFAULT
        )

    def generate_cover_letter(self, injected_prompt=None) -> str:
        """Generate cover-letter text from the configured job_post + resume.

        Returns the content string only. Persistence is the caller's
        responsibility — the POST view pre-creates a pending CoverLetter
        row and updates it with this content on completion. An earlier
        version of this method did its own get_or_create() here, which
        created a second row alongside the view's pending one whenever
        the generated content didn't match an existing empty row.
        """
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

        kwargs = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You write cover letters for a job seeker, in their own "
                        "voice. Output only the letter text.\n"
                        "\n"
                        "Ground the letter in the specific evidence provided — "
                        "name the actual employer, project, tool, or outcome "
                        "from the candidate's history rather than describing "
                        "the shape of one. Never invent an experience, metric, "
                        "or credential. Connect their real work to this "
                        "specific role and company; a letter that could be sent "
                        "to any employer has failed."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        }
        # Newer OpenAI models (the gpt-5 line, o-series) accept only the
        # default temperature and 400 on an explicit one — verified against
        # gpt-5 on 2026-08-12. Retry without it rather than failing the
        # generation, and remember the rejection so the wasted round-trip
        # happens once per process, not on every cover letter. Same guard as
        # AnswerService._call_ai.
        if not rejects_temperature(self.model):
            kwargs["temperature"] = 0.7
        try:
            completion = self.ai_client.chat.completions.create(**kwargs)
        except Exception as exc:
            if "temperature" not in kwargs or not is_temperature_error(exc):
                raise
            note_temperature_rejected(self.model)
            kwargs.pop("temperature", None)
            completion = self.ai_client.chat.completions.create(**kwargs)
        return completion.choices[0].message.content.strip()
