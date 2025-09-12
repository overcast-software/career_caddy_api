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
)


def _to_primitive(val):
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    return val


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
}
