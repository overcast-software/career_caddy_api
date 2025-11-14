import os
from io import BytesIO
from typing import Optional
from job_hunting.lib.models import Resume


class ResumeExportService:
    def __init__(self, default_template_path: Optional[str] = None):
        self.default_template_path = default_template_path
        if self.default_template_path is None:
            try:
                from django.conf import settings

                self.default_template_path = getattr(
                    settings,
                    "RESUME_EXPORT_TEMPLATE",
                    os.path.join(settings.BASE_DIR, "templates", "resume_export.docx"),
                )
            except Exception:
                self.default_template_path = "templates/resume_export.docx"

    def build_context(self, resume: Resume) -> dict:
        context = resume.to_export_context()

        # Post-process skills to group by skill_type
        skills = context["skills"]
        skills_by_type = {}

        for skill in skills:
            text = skill["text"]
            skill_type = skill["skill_type"]
            key = skill_type if skill_type and skill_type.strip() else "uncategorized"

            # Add to grouped dictionary
            if key not in skills_by_type:
                skills_by_type[key] = []
            skills_by_type[key].append(text)

        context["skills"] = skills_by_type

        return context

    def render_docx(self, resume: Resume, template_path: Optional[str] = None) -> bytes:
        try:
            from docxtpl import DocxTemplate
        except ImportError:
            raise ImportError("DOCX export requires 'docxtpl' to be installed")

        # Resolve template path
        path = template_path or self.default_template_path
        if not path:
            raise ValueError("No template path provided")

        key_mapping = {
            "Language": "language_skills",
            "Framework": "framework_skills",
            "Database": "database_skills",
            "Tool/Platform": "tool_skills",
            "Security": "security_skills",
        }
        # Load template and render
        template = DocxTemplate(path)
        context = self.build_context(resume)
        context["skills"] = {
            key_mapping.get(k): ", ".join(v) for k, v in context["skills"].items()
        }
        for new_key, skills in context["skills"].items():
            context[new_key] = skills
        try:
            template.render(context)
        except Exception as e:
            raise ValueError(f"Template rendering failed: {e}")

        # Save to BytesIO and return bytes
        buf = BytesIO()
        template.save(buf)
        buf.seek(0)
        return buf.getvalue()
