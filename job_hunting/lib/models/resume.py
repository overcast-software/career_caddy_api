from sqlalchemy import Column, Integer, Text, ForeignKey, String
from sqlalchemy.orm import relationship
from .base import BaseModel, Base


class Resume(BaseModel):
    __tablename__ = "resume"
    id = Column(Integer, primary_key=True, autoincrement=True)
    content = Column(Text)  # not using anymore
    user_id = Column(Integer, ForeignKey("user.id"))
    file_path = Column(String)
    title = Column(String)
    # Relationships
    user = relationship("User", back_populates="resumes")
    scores = relationship("Score", back_populates="resume")
    cover_letters = relationship("CoverLetter", back_populates="resume")
    applications = relationship("Application", back_populates="resume")
    summaries = relationship(
        "Summary",
        secondary="resume_summaries",
        back_populates="resumes",
        overlaps="resume_summaries,summary",
        passive_deletes=True,
    )

    resume_summaries = relationship(
        "ResumeSummaries",
        back_populates="resume",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    experiences = relationship(
        "Experience",
        secondary="resume_experience",
        back_populates="resumes",
        overlaps="experience,resume",
        order_by=lambda: Base.metadata.tables["resume_experience"].c.order,
    )

    certifications = relationship(
        "Certification",
        secondary="resume_certification",
        back_populates="resumes",
    )

    educations = relationship(
        "Education",
        secondary="resume_education",
        back_populates="resumes",
    )

    skills = relationship(
        "Skill",
        secondary="resume_skill",
        back_populates="resumes",
    )

    resume_skills = relationship(
        "ResumeSkill",
        back_populates="resume",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @classmethod
    def from_path_and_user_id(cls, path, user_id):
        with open(path) as file:
            body = file.read()
            resume, _ = cls.first_or_create(
                content=body, file_path=path, user_id=user_id
            )
        return resume

    def collated_content(self):
        parts = []

        # Active summary (if any)
        try:
            active_link = next(
                (l for l in (self.resume_summaries or []) if getattr(l, "active", False)),
                None,
            )
            if active_link and getattr(active_link, "summary", None):
                summary_text = (active_link.summary.content or "").strip()
                if summary_text:
                    parts.append("Summary:\n" + summary_text)
        except Exception:
            pass

        # Experiences
        try:
            exp_sections = []
            for exp in (self.experiences or []):
                header_bits = []
                if getattr(exp, "title", None):
                    header_bits.append(str(exp.title))
                company_name = ""
                try:
                    if getattr(exp, "company", None) and getattr(exp.company, "name", None):
                        company_name = exp.company.name
                except Exception:
                    company_name = ""
                if company_name:
                    header_bits.append(company_name)
                if getattr(exp, "location", None):
                    header_bits.append(str(exp.location))

                date_bits = []
                if getattr(exp, "start_date", None):
                    date_bits.append(str(exp.start_date))
                if getattr(exp, "end_date", None):
                    date_bits.append(str(exp.end_date))
                date_range = " - ".join(date_bits) if date_bits else ""

                header = " | ".join([b for b in header_bits if b]) + (f" ({date_range})" if date_range else "")

                # Descriptions (ordered if possible)
                lines = []
                try:
                    if getattr(exp, "descriptions", None):
                        lines = [d.content.strip() for d in exp.descriptions if getattr(d, "content", None)]
                except Exception:
                    lines = []
                if not lines and getattr(exp, "content", None):
                    raw = exp.content or ""
                    lines = [ln.strip() for ln in raw.splitlines() if ln and ln.strip()]

                body = "\n".join(f"- {ln}" for ln in lines) if lines else ""
                section = header if header else ""
                if body:
                    section = (section + "\n" if section else "") + body
                if section:
                    exp_sections.append(section)
            if exp_sections:
                parts.append("Experience:\n" + "\n\n".join(exp_sections))
        except Exception:
            pass

        # Education
        try:
            edu_lines = []
            for edu in (self.educations or []):
                bits = []
                if getattr(edu, "degree", None):
                    bits.append(str(edu.degree))
                if getattr(edu, "institution", None):
                    bits.append(str(edu.institution))
                majors = []
                if getattr(edu, "major", None):
                    majors.append(str(edu.major))
                if getattr(edu, "minor", None):
                    majors.append(f"Minor: {edu.minor}")
                if majors:
                    bits.append(", ".join(majors))
                if getattr(edu, "issue_date", None):
                    bits.append(str(edu.issue_date))
                line = " | ".join([b for b in bits if b])
                if line:
                    edu_lines.append(line)
            if edu_lines:
                parts.append("Education:\n" + "\n".join(edu_lines))
        except Exception:
            pass

        # Certifications
        try:
            cert_lines = []
            for cert in (self.certifications or []):
                bits = []
                if getattr(cert, "title", None):
                    bits.append(str(cert.title))
                if getattr(cert, "issuer", None):
                    bits.append(str(cert.issuer))
                if getattr(cert, "issue_date", None):
                    bits.append(str(cert.issue_date))
                line = " | ".join([b for b in bits if b])
                if line:
                    cert_lines.append(line)
            if cert_lines:
                parts.append("Certifications:\n" + "\n".join(cert_lines))
        except Exception:
            pass

        return "\n\n".join([p for p in parts if p])
