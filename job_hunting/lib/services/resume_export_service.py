import os
import re
from io import BytesIO
from typing import Optional
from job_hunting.models import Resume


# Historical aliases the legacy DOCX template references. The naming is
# inconsistent (Tool/Platform → tool_skills, not tool_platform_skills) so
# we can't compute these — they have to match the binary template verbatim.
_LEGACY_SKILL_KEY_ALIASES = {
    "Language": "language_skills",
    "Framework": "framework_skills",
    "Database": "database_skills",
    "Tool/Platform": "tool_skills",
    "Security": "security_skills",
}


def _legacy_skill_key(skill_type: str) -> str:
    """Derive a snake_case context key from a free-form skill_type.

    Returns the historical alias when one exists (so the legacy DOCX
    template's {{ language_skills }} etc. placeholders still resolve),
    otherwise slugifies the input. Empty input falls back to
    'other_skills'.
    """
    if not skill_type or not skill_type.strip():
        return "other_skills"
    if skill_type in _LEGACY_SKILL_KEY_ALIASES:
        return _LEGACY_SKILL_KEY_ALIASES[skill_type]
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", skill_type.strip().lower()).strip("_")
    return f"{slug}_skills" if slug else "other_skills"


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

        # Load template and render
        template = DocxTemplate(path)
        context = self.build_context(resume)
        # Surface every observed skill_type on the context under a derived
        # key — `Language` → `language_skills`, `Tool/Platform` →
        # `tool_platform_skills`, `Project Management` →
        # `project_management_skills`. The legacy DOCX template references
        # the dev-era keys (language_skills, framework_skills, …) so they
        # still resolve; non-dev categories are present on the context but
        # the binary template won't render them until M4 reorganizes it.
        context["skills"] = {
            _legacy_skill_key(k): ", ".join(v) for k, v in context["skills"].items()
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
