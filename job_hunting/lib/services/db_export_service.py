from jinja2 import Environment, FileSystemLoader
from job_hunting.lib.models import Resume, Skill, ResumeSkill


class DbExportService:
    def __init__(self):
        self.env = Environment(loader=FileSystemLoader("templates"))

    def _build_skills_list(self, resume):
        """Build a normalized list of skills for the resume, preferring active skills."""
        skills = []
        try:
            session = Resume.get_session()
            links = session.query(ResumeSkill).filter_by(resume_id=resume.id).all()
            
            # Check if any links have active attribute set
            has_active_flags = any(getattr(link, 'active', None) is not None for link in links)
            
            # Filter to active only if active flags exist, otherwise include all
            if has_active_flags:
                links = [link for link in links if getattr(link, 'active', False)]
            
            seen = set()
            for link in links:
                skill = None
                try:
                    skill = getattr(link, 'skill', None)
                    if skill is None:
                        skill = Skill.get(link.skill_id)
                except Exception:
                    continue
                
                if skill is None:
                    continue
                
                # Get display value
                try:
                    if hasattr(skill, 'to_export_value') and callable(skill.to_export_value):
                        value = skill.to_export_value()
                    else:
                        value = getattr(skill, 'text', '')
                except Exception:
                    value = getattr(skill, 'text', '')
                
                # Normalize and deduplicate
                value = str(value).strip()
                if value and value not in seen:
                    seen.add(value)
                    skills.append(value)
                    
        except Exception:
            # Fallback to resume.skills if join query fails
            try:
                resume_skills = getattr(resume, 'skills', []) or []
                seen = set()
                for skill in resume_skills:
                    try:
                        if hasattr(skill, 'to_export_value') and callable(skill.to_export_value):
                            value = skill.to_export_value()
                        else:
                            value = getattr(skill, 'text', '')
                    except Exception:
                        value = getattr(skill, 'text', '')
                    
                    value = str(value).strip()
                    if value and value not in seen:
                        seen.add(value)
                        skills.append(value)
            except Exception:
                pass
        
        return skills

    def resume_markdown_export(self, resume: Resume) -> str:
        skills = self._build_skills_list(resume)
        
        try:
            template = self.env.get_template("resume_markdown.j2")
            return template.render(resume=resume, skills=skills).strip()
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
