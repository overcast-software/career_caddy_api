from datetime import datetime, date
from typing import Any, Dict, List
from job_hunting.lib.models import (
    User,
    Resume,
    Score,
    JobPost,
    Scrape,
    Company,
    CoverLetter,
    Application,
    Summary,
    Experience,
    Education,
    Certification,
    Description,
    ExperienceDescription,
)


def _to_primitive(val):
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    return val


def _parse_date(val):
    if val is None or val == "":
        return None
    if isinstance(val, (datetime, date)):
        return val.date() if isinstance(val, datetime) else val
    try:
        return date.fromisoformat(str(val))
    except Exception:
        return None


def _pluralize_type(t: str) -> str:
    # Minimal pluralization for our resource type names
    if t.endswith("y") and not t.endswith(("ay", "ey", "iy", "oy", "uy")):
        return t[:-1] + "ies"
    if t.endswith("s"):
        return t + "es"
    return t + "s"


class BaseSASerializer:
    type: str
    model: Any
    attributes: List[str] = []
    relationships: Dict[str, Dict[str, Any]] = {}
    relationship_fks: Dict[str, str] = {}

    def set_parent_context(self, parent_type: str, parent_id: int, rel_name: str):
        self._parent_context = {
            "parent_type": parent_type,
            "parent_id": parent_id,
            "rel_name": rel_name,
        }

    def accepted_types(self):
        return {self.type, _pluralize_type(self.type)}

    def to_resource(self, obj) -> Dict[str, Any]:
        res = {
            "type": self.type,
            "id": str(obj.id),
            "attributes": {k: _to_primitive(getattr(obj, k)) for k in self.attributes},
        }
        if self.relationships:
            rel_out = {}
            for rel_name, cfg in self.relationships.items():
                rel_attr = cfg["attr"]
                rel_type = cfg["type"]
                uselist = cfg.get("uselist", True)
                target = getattr(obj, rel_attr, None)
                if uselist:
                    data = [{"type": rel_type, "id": str(i.id)} for i in (target or [])]
                else:
                    data = {"type": rel_type, "id": str(target.id)} if target else None
                rel_out[rel_name] = {"data": data}
            res["relationships"] = rel_out
        return res

    def get_related(self, obj, rel_name):
        cfg = self.relationships.get(rel_name)
        if not cfg:
            return None, []
        attr = cfg["attr"]
        rel_type = cfg["type"]
        uselist = cfg.get("uselist", True)
        target = getattr(obj, attr, None)
        if target is None:
            return rel_type, []
        items = list(target) if uselist else [target]
        return rel_type, items

    def parse_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict) or "data" not in payload:
            raise ValueError("JSON:API payload must contain 'data'")
        data = payload["data"]
        expected = self.accepted_types()
        if data.get("type") not in expected:
            exp_str = "', '".join(sorted(expected))
            raise ValueError(f"JSON:API type mismatch: expected one of '{exp_str}'")
        attrs_in = data.get("attributes", {}) or {}
        out: Dict[str, Any] = {}
        for k in self.attributes:
            if k in attrs_in:
                out[k] = attrs_in[k]
        rels = data.get("relationships", {}) or {}
        for rel_name, fk_field in self.relationship_fks.items():
            rel = rels.get(rel_name)
            if rel and isinstance(rel.get("data"), dict):
                out[fk_field] = int(rel["data"]["id"])
            elif rel and rel.get("data") is None:
                out[fk_field] = None
        return out


class UserSerializer(BaseSASerializer):
    type = "user"
    model = User
    attributes = ["name", "email"]
    relationships = {
        "resumes": {"attr": "resumes", "type": "resume", "uselist": True},
        "scores": {"attr": "scores", "type": "score", "uselist": True},
        "cover-letters": {
            "attr": "cover_letters",
            "type": "cover-letter",
            "uselist": True,
        },
        "applications": {
            "attr": "applications",
            "type": "application",
            "uselist": True,
        },
        "summaries": {"attr": "summaries", "type": "summary", "uselist": True},
    }


class ResumeSerializer(BaseSASerializer):
    type = "resume"
    model = Resume
    attributes = ["content", "file_path"]
    relationships = {
        "user": {"attr": "user", "type": "user", "uselist": False},
        "scores": {"attr": "scores", "type": "score", "uselist": True},
        "cover-letters": {
            "attr": "cover_letters",
            "type": "cover-letter",
            "uselist": True,
        },
        "applications": {
            "attr": "applications",
            "type": "application",
            "uselist": True,
        },
        "summaries": {"attr": "summaries", "type": "summary", "uselist": True},
        "experiences": {
            "attr": "experiences",
            "type": "experience",
            "uselist": True,
        },
        "educations": {"attr": "educations", "type": "education", "uselist": True},
        "certifications": {
            "attr": "certifications",
            "type": "certification",
            "uselist": True,
        },
    }
    relationship_fks = {"user": "user_id"}


class ScoreSerializer(BaseSASerializer):
    type = "score"
    model = Score
    attributes = ["score", "explanation"]
    relationships = {
        "resume": {"attr": "resume", "type": "resume", "uselist": False},
        "job-post": {"attr": "job_post", "type": "job-post", "uselist": False},
        "user": {"attr": "user", "type": "user", "uselist": False},
    }
    relationship_fks = {
        "resume": "resume_id",
        "job-post": "job_post_id",
        "user": "user_id",
    }


class JobPostSerializer(BaseSASerializer):
    type = "job-post"
    model = JobPost
    attributes = [
        "description",
        "title",
        "posted_date",
        "extraction_date",
        "created_at",
    ]
    relationships = {
        "company": {"attr": "company", "type": "company", "uselist": False},
        "scores": {"attr": "scores", "type": "score", "uselist": True},
        "scrapes": {"attr": "scrapes", "type": "scrape", "uselist": True},
        "cover-letters": {
            "attr": "cover_letters",
            "type": "cover-letter",
            "uselist": True,
        },
        "applications": {
            "attr": "applications",
            "type": "application",
            "uselist": True,
        },
        "summaries": {"attr": "summaries", "type": "summary", "uselist": True},
    }
    relationship_fks = {"company": "company_id"}


class ScrapeSerializer(BaseSASerializer):
    type = "scrape"
    model = Scrape
    attributes = [
        "url",
        "css_selectors",
        "job_content",
        "external_link",
        "parse_method",
        "scraped_at",
        "state",
        "html",
    ]
    relationships = {
        "job-post": {"attr": "job_post", "type": "job-post", "uselist": False},
        "company": {"attr": "company", "type": "company", "uselist": False},
    }
    relationship_fks = {"job-post": "job_post_id", "company": "company_id"}


class CompanySerializer(BaseSASerializer):
    type = "company"
    model = Company
    attributes = ["name", "display_name"]
    relationships = {
        "job-posts": {"attr": "job_posts", "type": "job-post", "uselist": True},
        "scrapes": {"attr": "scrapes", "type": "scrape", "uselist": True},
    }


class CoverLetterSerializer(BaseSASerializer):
    type = "cover-letter"
    model = CoverLetter
    attributes = ["content", "created_at"]
    relationships = {
        "user": {"attr": "user", "type": "user", "uselist": False},
        "resume": {"attr": "resume", "type": "resume", "uselist": False},
        "job-post": {"attr": "job_post", "type": "job-post", "uselist": False},
        "application": {"attr": "application", "type": "application", "uselist": False},
    }
    relationship_fks = {
        "user": "user_id",
        "resume": "resume_id",
        "job-post": "job_post_id",
    }


class ApplicationSerializer(BaseSASerializer):
    type = "application"
    model = Application
    attributes = ["applied_at", "status", "tracking_url", "notes"]
    relationships = {
        "user": {"attr": "user", "type": "user", "uselist": False},
        "job-post": {"attr": "job_post", "type": "job-post", "uselist": False},
        "resume": {"attr": "resume", "type": "resume", "uselist": False},
        "cover-letter": {
            "attr": "cover_letter",
            "type": "cover-letter",
            "uselist": False,
        },
    }
    relationship_fks = {
        "user": "user_id",
        "job-post": "job_post_id",
        "resume": "resume_id",
        "cover-letter": "cover_letter_id",
    }


class SummarySerializer(BaseSASerializer):
    type = "summary"
    model = Summary
    attributes = ["content"]
    relationships = {
        "user": {"attr": "user", "type": "user", "uselist": False},
        "job-post": {"attr": "job_post", "type": "job-post", "uselist": False},
        "resume": {"attr": "resume", "type": "resume", "uselist": False},
    }
    relationship_fks = {
        "user": "user_id",
        "job-post": "job_post_id",
        "resume": "resume_id",
    }


class ExperienceSerializer(BaseSASerializer):
    type = "experience"
    model = Experience
    attributes = ["title", "start_date", "end_date", "summary", "location", "content"]
    relationships = {
        "resumes": {"attr": "resumes", "type": "resume", "uselist": True},
        "company": {"attr": "company", "type": "company", "uselist": False},
        "descriptions": {"attr": "descriptions", "type": "description", "uselist": True},
    }
    relationship_fks = {"company": "company_id"}

    def to_resource(self, obj):
        res = super().to_resource(obj)
        ctx = getattr(self, "_parent_context", None)
        if ctx and ctx.get("parent_type") == "resume":
            res.setdefault("attributes", {})["resume_id"] = ctx.get("parent_id")
        else:
            rid = None
            try:
                if getattr(obj, "resumes", None):
                    rid = obj.resumes[0].id
            except Exception:
                rid = None
            if rid is not None:
                res.setdefault("attributes", {})["resume_id"] = rid

        # Also expose description content lines for convenience, either from linked descriptions
        # or by splitting the legacy Experience.content field.
        try:
            desc_list = getattr(obj, "descriptions", None)
            if desc_list:
                lines = [d.content for d in desc_list if getattr(d, "content", None)]
            else:
                raw = getattr(obj, "content", None) or ""
                lines = [ln.strip() for ln in raw.splitlines() if ln and ln.strip()]
            if lines:
                res.setdefault("attributes", {})["description_lines"] = lines
        except Exception:
            # Non-fatal; just omit description_lines on error
            pass

        return res

    def parse_payload(self, payload):
        out = super().parse_payload(payload)
        if "start_date" in out:
            out["start_date"] = _parse_date(out["start_date"])
        if "end_date" in out:
            out["end_date"] = _parse_date(out["end_date"])
        return out


class EducationSerializer(BaseSASerializer):
    type = "education"
    model = Education
    attributes = ["degree", "issue_date", "institution", "major", "minor"]
    relationships = {
        "resumes": {"attr": "resumes", "type": "resume", "uselist": True},
    }

    def to_resource(self, obj):
        res = super().to_resource(obj)
        ctx = getattr(self, "_parent_context", None)
        if ctx and ctx.get("parent_type") == "resume":
            res.setdefault("attributes", {})["resume_id"] = ctx.get("parent_id")
        return res

    def parse_payload(self, payload):
        out = super().parse_payload(payload)
        if "issue_date" in out:
            out["issue_date"] = _parse_date(out["issue_date"])
        return out


class CertificationSerializer(BaseSASerializer):
    type = "certification"
    model = Certification
    attributes = ["issuer", "title", "issue_date", "content"]
    relationships = {
        "resumes": {"attr": "resumes", "type": "resume", "uselist": True},
    }

    def to_resource(self, obj):
        res = super().to_resource(obj)
        ctx = getattr(self, "_parent_context", None)
        if ctx and ctx.get("parent_type") == "resume":
            res.setdefault("attributes", {})["resume_id"] = ctx.get("parent_id")
        else:
            rid = None
            try:
                if getattr(obj, "resumes", None):
                    rid = obj.resumes[0].id
            except Exception:
                rid = None
            if rid is not None:
                res.setdefault("attributes", {})["resume_id"] = rid
        return res

    def parse_payload(self, payload):
        out = super().parse_payload(payload)
        if "issue_date" in out:
            out["issue_date"] = _parse_date(out["issue_date"])
        return out


class DescriptionSerializer(BaseSASerializer):
    type = "description"
    model = Description
    attributes = ["content"]
    relationships = {
        "experiences": {"attr": "experiences", "type": "experience", "uselist": True},
    }

    def to_resource(self, obj):
        res = super().to_resource(obj)
        # If included under an experience, inject per-link 'order' from the join table
        try:
            ctx = getattr(self, "_parent_context", None)
            if ctx and ctx.get("parent_type") == "experience":
                experience_id = ctx.get("parent_id")
                if experience_id:
                    session = self.model.get_session()
                    link = (
                        session.query(ExperienceDescription)
                        .filter_by(experience_id=int(experience_id), description_id=obj.id)
                        .first()
                    )
                    if link and hasattr(link, "order"):
                        res.setdefault("attributes", {})["order"] = link.order
        except Exception:
            # Non-fatal; omit 'order' if unavailable
            pass
        return res


TYPE_TO_SERIALIZER = {
    "user": UserSerializer,
    "resume": ResumeSerializer,
    "score": ScoreSerializer,
    "job-post": JobPostSerializer,
    "scrape": ScrapeSerializer,
    "company": CompanySerializer,
    "cover-letter": CoverLetterSerializer,
    "application": ApplicationSerializer,
    "summary": SummarySerializer,
    "experience": ExperienceSerializer,
    "education": EducationSerializer,
    "certification": CertificationSerializer,
    "description": DescriptionSerializer,
}
