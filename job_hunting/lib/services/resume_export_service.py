import os
from io import BytesIO
from docxtpl import DocxTemplate
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
        return resume.to_export_context()

    def render_docx(self, resume: Resume, template_path: Optional[str] = None) -> bytes:
        try:
            from docxtpl import DocxTemplate
        except ImportError:
            raise ImportError("DOCX export requires 'docxtpl' to be installed")

        # Resolve template path
        path = template_path or self.default_template_path
        if not path:
            raise ValueError("No template path provided")

        # Load template and render
        template = DocxTemplate(path)
        context = self.build_context(resume)

        try:
            template.render(context)
        except Exception as e:
            breakpoint()

        # Save to BytesIO and return bytes
        buf = BytesIO()
        template.save(buf)
        buf.seek(0)
        return buf.getvalue()
