from jinja2 import Environment, FileSystemLoader
from job_hunting.lib.models import Resume


class DbExportService:
    def __init__(self):
        self.env = Environment(loader=FileSystemLoader("templates"))

    def resume_markdown_export(self, resume: Resume) -> str:
        try:
            template = self.env.get_template("resume_markdown.j2")
            return template.render(resume=resume).strip()
        except Exception:
            # Fallback to programmatic builder
            pass

        lines = []

        # Header
        user = getattr(resume, "user", None)
        name = getattr(user, "name", None)
        title = getattr(resume, "title", None)
        header = " | ".join([s for s in [name, title] if s])
        if header:
            lines.append(f"# {header}")

        # Active summary (if available)
        try:
            links = list(getattr(resume, "resume_summaries", []) or [])
            active_link = next((l for l in links if getattr(l, "active", False)), None)
            if active_link and getattr(active_link, "summary", None):
                content = getattr(active_link.summary, "content", None)
                if content:
                    lines.append("## Summary")
                    lines.append(str(content).strip())
        except Exception:
            pass

        # Experiences
        exps = list(getattr(resume, "experiences", []) or [])
        if exps:
            lines.append("## Experience")
            for exp in exps:
                company = getattr(getattr(exp, "company", None), "name", None)
                etitle = getattr(exp, "title", None)
                loc = getattr(exp, "location", None)
                parts = [p for p in [etitle, company, loc] if p]
                if parts:
                    lines.append(f"### {' - '.join(parts)}")
                for d in list(getattr(exp, "descriptions", []) or []):
                    content = getattr(d, "content", None)
                    if content:
                        lines.append(f"- {str(content).strip()}")

        # Education
        edus = list(getattr(resume, "educations", []) or [])
        if edus:
            lines.append("## Education")
            for edu in edus:
                inst = getattr(edu, "institution", None)
                degree = getattr(edu, "degree", None)
                major = getattr(edu, "major", None)
                parts = [p for p in [inst, degree, major] if p]
                if parts:
                    lines.append(f"- {' | '.join(parts)}")

        # Certifications
        certs = list(getattr(resume, "certifications", []) or [])
        if certs:
            lines.append("## Certifications")
            for cert in certs:
                issuer = getattr(cert, "issuer", None)
                ctitle = getattr(cert, "title", None)
                parts = [p for p in [issuer, ctitle] if p]
                if parts:
                    lines.append(f"- {' | '.join(parts)}")

        markdown = "\n".join(lines).strip()
        return markdown or (getattr(resume, "content", "") or "")
