"""Presentation-ready context for the resume Word export template.

Design goal: the .docx template contains only `{{ placeholders }}`, `{% for %}`
loops, and docxtpl's paragraph-level `{%p if %} ... {%p endif %}` tags around
section headings. No inline `{% if %}`, no whitespace-control markers — those
are the constructs that docxtpl splits across Word runs and then fails to
re-parse.

All branching lives here, in Python. The template just renders the shape.
"""
from typing import Optional

from job_hunting.models import Resume


SKILL_TYPE_LABELS = [
    ("Language", "Languages"),
    ("Database", "Databases"),
    ("Framework", "Frameworks"),
    ("Tool/Platform", "Platforms"),
    ("Security", "Security"),
]


def _join_nonempty(parts, sep=" — "):
    return sep.join(p for p in parts if p)


def _fmt_date_range(start, end) -> str:
    if start and end:
        return f"{start} – {end}"
    if start:
        return f"{start} – Present"
    if end:
        return str(end)
    return ""


def _header(resume: Resume) -> dict:
    user = getattr(resume, "user", None)
    name = ""
    email = ""
    if user is not None:
        try:
            name = user.get_full_name() or ""
        except Exception:
            name = ""
        email = getattr(user, "email", "") or ""

    phone = resume.user_phone or ""
    contact_parts = [email, phone]
    contact = " * ".join(p for p in contact_parts if p)

    return {
        "header_name": name,
        "header_title": resume.title or "",
        "header_contact": contact,
    }


def _summary(resume: Resume) -> dict:
    body = (resume.active_summary_content() or "").strip()
    return {
        "summary_body": body,
        "has_summary": bool(body),
    }


def _skills(resume: Resume) -> dict:
    # Bucket skills by canonical skill_type strings (matches SkillTag enum).
    buckets: dict[str, list[str]] = {key: [] for key, _ in SKILL_TYPE_LABELS}
    for s in resume.skills:
        text = (s.text or "").strip()
        if not text:
            continue
        if s.skill_type in buckets:
            buckets[s.skill_type].append(text)

    lines = []
    for key, label in SKILL_TYPE_LABELS:
        values = buckets[key]
        if values:
            lines.append(f"{label}: {', '.join(values)}")

    return {
        "skills_lines": lines,
        "has_skills": bool(lines),
    }


def _experiences(resume: Resume) -> dict:
    rows = []
    for e in resume.experiences:
        company_name = e.company.name if e.company_id else ""
        header_line = _join_nonempty(
            [
                e.title or "",
                company_name,
                e.location or "",
                _fmt_date_range(e.start_date, e.end_date),
            ]
        )

        # Pre-strip description bullets — no downstream filtering needed.
        from job_hunting.models.experience_description import (
            ExperienceDescription,
        )
        from job_hunting.models.description import Description

        desc_ids = list(
            ExperienceDescription.objects.filter(experience_id=e.id)
            .order_by("order")
            .values_list("description_id", flat=True)
        )
        desc_map = {d.id: d for d in Description.objects.filter(pk__in=desc_ids)}
        bullets = [
            str(desc_map[did].content).strip()
            for did in desc_ids
            if did in desc_map and desc_map[did].content
        ]

        rows.append(
            {
                "header_line": header_line,
                "summary": (e.summary or "").strip(),
                "descriptions": bullets,
            }
        )

    return {
        "experiences": rows,
        "has_experiences": bool(rows),
    }


def _certifications(resume: Resume) -> dict:
    rows = []
    for c in resume.certifications:
        header_line = _join_nonempty(
            [
                c.title or "",
                c.issuer or "",
                c.issue_date.isoformat() if c.issue_date else "",
            ]
        )
        rows.append(
            {
                "header_line": header_line,
                "content": (c.content or "").strip(),
            }
        )
    return {
        "certifications": rows,
        "has_certifications": bool(rows),
    }


def _educations(resume: Resume) -> dict:
    rows = []
    for e in resume.educations:
        parts = [e.institution or "", e.degree or "", e.major or ""]
        if e.minor:
            parts.append(f"Minor: {e.minor}")
        if e.issue_date:
            parts.append(e.issue_date.isoformat())
        rows.append({"line": _join_nonempty(parts)})
    return {
        "educations": rows,
        "has_educations": bool(rows),
    }


def _projects(resume: Resume) -> dict:
    rows = []
    for p in resume.projects:
        header_line = _join_nonempty(
            [
                p.title or "",
                _fmt_date_range(p.start_date, p.end_date),
            ]
        )
        bullets = []
        for d in p.descriptions:
            if d.content:
                bullets.append(str(d.content).strip())
        rows.append(
            {
                "header_line": header_line,
                "descriptions": bullets,
            }
        )
    return {
        "projects": rows,
        "has_projects": bool(rows),
    }


def build_context(resume: Resume) -> dict:
    """Return a flat, presentation-ready context dict for docxtpl.

    Every branching decision has already been made; the template renders
    a fixed shape with `{% for %}` loops (empty lists render nothing) and
    docxtpl paragraph-level `{%p if has_X %}Heading{%p endif %}` for the
    section titles.
    """
    ctx: dict = {}
    ctx.update(_header(resume))
    ctx.update(_summary(resume))
    ctx.update(_skills(resume))
    ctx.update(_experiences(resume))
    ctx.update(_certifications(resume))
    ctx.update(_educations(resume))
    ctx.update(_projects(resume))
    return ctx


def render_docx(
    resume: Resume, template_path: Optional[str] = None
) -> bytes:
    """Render the resume to docx bytes using the smart-Python context.

    Mirrors ResumeExportService.render_docx signature so callers can swap in
    this function without touching the viewset. Defaults to the same
    template location as the legacy service.
    """
    import os
    from io import BytesIO

    from django.conf import settings
    from docxtpl import DocxTemplate

    path = template_path or getattr(
        settings,
        "RESUME_EXPORT_TEMPLATE",
        os.path.join(settings.BASE_DIR, "templates", "resume_export.docx"),
    )

    template = DocxTemplate(path)
    template.render(build_context(resume))

    buf = BytesIO()
    template.save(buf)
    buf.seek(0)
    return buf.getvalue()
