from datetime import date, datetime, timezone
from typing import Any, Dict, List

import dateparser
from django.contrib.auth import get_user_model

from job_hunting.lib.models import (
    Application,
    Certification,
    Company,
    CoverLetter,
    Description,
    Education,
    Experience,
    ExperienceDescription,
    JobApplicationStatus,
    JobPost,
    Answer,
    Question,
    Resume,
    ResumeSkill,
    ResumeSummaries,
    Score,
    Scrape,
    Skill,
    Status,
    Summary,
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


def _parse_datetime(val):
    if val is None or val == "":
        return None
    if isinstance(val, datetime):
        # If timezone-aware, convert to UTC and make naive
        if val.tzinfo is not None:
            val = val.astimezone(timezone.utc).replace(tzinfo=None)
        return val
    try:
        dt = dateparser.parse(str(val))
        if dt and dt.tzinfo is not None:
            # Convert to UTC and make naive
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _pluralize_type(t: str) -> str:
    # Minimal pluralization for our resource type names
    if t.endswith("y") and not t.endswith(("ay", "ey", "iy", "oy", "uy")):
        return t[:-1] + "ies"
    if t.endswith("s"):
        return t + "es"
    return t + "s"


# Route prefix mapping for special cases
ROUTE_PREFIX_BY_TYPE = {
    "application": "job-applications",
    "job-application": "job-applications",
    "job-applications": "job-applications",
    "company": "companies",
}


def _resource_base_path(t: str) -> str:
    # Assumes your API is mounted at /api/v1/
    if t in ROUTE_PREFIX_BY_TYPE:
        return f"/api/v1/{ROUTE_PREFIX_BY_TYPE[t]}"
    return f"/api/v1/{_pluralize_type(t)}"


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
        # JSON:API resource self link
        res["links"] = {"self": f"{_resource_base_path(self.type)}/{obj.id}"}
        if self.relationships:
            rel_out = {}
            for rel_name, cfg in self.relationships.items():
                rel_attr = cfg["attr"]
                rel_type = cfg["type"]
                uselist = cfg.get("uselist", True)
                target = getattr(obj, rel_attr, None)
                if uselist:
                    data = [{"type": rel_type, "id": str(i.id)} for i in (target or [])]
                    # Map relationship name to URL segment for special cases
                    rel_segment = (
                        "job-applications" if rel_name == "applications" else rel_name
                    )
                    rel_out[rel_name] = {
                        "data": data,
                        "links": {
                            "self": f"{_resource_base_path(self.type)}/{obj.id}/relationships/{rel_name}",
                            "related": f"{_resource_base_path(self.type)}/{obj.id}/{rel_segment}",
                        },
                    }
                else:
                    # Determine target_id with FK fallback
                    target_id = None
                    if target is not None and getattr(target, "id", None) is not None:
                        target_id = target.id
                    else:
                        # FK fallback: check if we have a foreign key field for this relationship
                        fk_field = self.relationship_fks.get(rel_name)
                        if fk_field:
                            fk_value = getattr(obj, fk_field, None)
                            if fk_value is not None:
                                target_id = fk_value

                    data = (
                        {"type": rel_type, "id": str(target_id)}
                        if target_id is not None
                        else None
                    )
                    links = {
                        "self": f"{_resource_base_path(self.type)}/{obj.id}/relationships/{rel_name}",
                    }
                    # Include related link if we have a target_id (even when target is None)
                    if target_id is not None:
                        links["related"] = (
                            f"{_resource_base_path(rel_type)}/{target_id}"
                        )
                    rel_out[rel_name] = {"data": data, "links": links}
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


class DjangoUserSerializer:
    type = "user"

    def accepted_types(self):
        return {self.type, _pluralize_type(self.type)}

    def to_resource(self, obj) -> Dict[str, Any]:
        # Fetch phone from SQLAlchemy Profile by user_id
        phone = ""
        try:
            from job_hunting.lib.models.profile import Profile

            session = Profile.get_session()
            prof = session.query(Profile).filter_by(user_id=obj.id).first()
            if prof and getattr(prof, "phone", None):
                phone = prof.phone or ""
        except Exception:
            phone = ""

        res = {
            "type": self.type,
            "id": str(obj.id),
            "attributes": {
                "username": obj.username,
                "email": obj.email or "",
                "first_name": obj.first_name or "",
                "last_name": obj.last_name or "",
                "phone": phone or "",
            },
        }
        res["links"] = {"self": f"{_resource_base_path(self.type)}/{obj.id}"}

        # Add relationships structure
        res["relationships"] = {
            "resumes": {
                "links": {
                    "self": f"{_resource_base_path(self.type)}/{obj.id}/relationships/resumes",
                    "related": f"{_resource_base_path(self.type)}/{obj.id}/resumes",
                },
            },
            "scores": {
                "links": {
                    "self": f"{_resource_base_path(self.type)}/{obj.id}/relationships/scores",
                    "related": f"{_resource_base_path(self.type)}/{obj.id}/scores",
                },
            },
            "cover-letters": {
                "links": {
                    "self": f"{_resource_base_path(self.type)}/{obj.id}/relationships/cover-letters",
                    "related": f"{_resource_base_path(self.type)}/{obj.id}/cover-letters",
                },
            },
            "applications": {
                "links": {
                    "self": f"{_resource_base_path(self.type)}/{obj.id}/relationships/applications",
                    "related": f"{_resource_base_path(self.type)}/{obj.id}/job-applications",
                },
            },
            "summaries": {
                "links": {
                    "self": f"{_resource_base_path(self.type)}/{obj.id}/relationships/summaries",
                    "related": f"{_resource_base_path(self.type)}/{obj.id}/summaries",
                },
            },
        }
        return res

    def get_related(self, obj, rel_name):
        # Import here to avoid circular imports
        from job_hunting.lib.models import (
            Application,
            CoverLetter,
            Resume,
            Score,
            Summary,
        )

        session = Resume.get_session()  # Get SA session

        if rel_name == "resumes":
            items = session.query(Resume).filter_by(user_id=obj.id).all()
            return "resume", items
        elif rel_name == "scores":
            items = session.query(Score).filter_by(user_id=obj.id).all()
            return "score", items
        elif rel_name == "cover-letters":
            items = session.query(CoverLetter).filter_by(user_id=obj.id).all()
            return "cover-letter", items
        elif rel_name == "applications":
            items = session.query(Application).filter_by(user_id=obj.id).all()
            return "job-application", items
        elif rel_name == "summaries":
            items = session.query(Summary).filter_by(user_id=obj.id).all()
            return "summary", items
        else:
            return None, []

    def parse_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # Accept both JSON:API and flat JSON payloads
        attrs_in: Dict[str, Any] = {}
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            data = payload["data"]
            expected = self.accepted_types()
            if data.get("type") not in expected:
                exp_str = "', '".join(sorted(expected))
                raise ValueError(f"JSON:API type mismatch: expected one of '{exp_str}'")
            attrs_in = (data.get("attributes") or {}) if isinstance(data, dict) else {}
        elif isinstance(payload, dict):
            # Flat JSON like {"username": "...", "email": "...", "password": "...", "phone": "..."}
            attrs_in = payload
        else:
            raise ValueError("Invalid payload")

        out: Dict[str, Any] = {}
        for k in ["username", "email", "first_name", "last_name", "password", "phone"]:
            if k in attrs_in:
                out[k] = attrs_in[k]
        return out


class ResumeSerializer(BaseSASerializer):
    type = "resume"
    model = Resume
    attributes = ["file_path", "title", "name", "notes", "user_id"]
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
            "type": "job-application",
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
        "skills": {"attr": "skills", "type": "skill", "uselist": True},
    }
    relationship_fks = {"user": "user_id"}

    def to_resource(self, obj):
        res = super().to_resource(obj)
        # Ensure user relationship linkage points to Django user
        if hasattr(obj, "user_id") and obj.user_id:
            res.setdefault("relationships", {})["user"] = {
                "data": {"type": "user", "id": str(obj.user_id)},
                "links": {
                    "self": f"{_resource_base_path(self.type)}/{obj.id}/relationships/user",
                    "related": f"{_resource_base_path('user')}/{obj.user_id}",
                },
            }
        # Convenience link to related summaries collection
        res.setdefault("links", {})[
            "summaries"
        ] = f"{_resource_base_path(self.type)}/{obj.id}/summaries"
        return res

    def get_related(self, obj, rel_name):
        if rel_name == "user" and hasattr(obj, "user_id") and obj.user_id:
            User = get_user_model()
            try:
                user = User.objects.get(id=obj.user_id)
                return "user", [user]
            except User.DoesNotExist:
                return "user", []
        elif rel_name == "company":
            # Try direct company relationship first
            if hasattr(obj, "company") and obj.company:
                return "company", [obj.company]
            # Fallback to job_post.company
            elif (
                hasattr(obj, "job_post")
                and obj.job_post
                and hasattr(obj.job_post, "company")
                and obj.job_post.company
            ):
                return "company", [obj.job_post.company]
            else:
                return "company", []
        return super().get_related(obj, rel_name)


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

    def to_resource(self, obj):
        res = super().to_resource(obj)
        # Ensure user relationship linkage points to Django user
        if hasattr(obj, "user_id") and obj.user_id:
            res.setdefault("relationships", {})["user"] = {
                "data": {"type": "user", "id": str(obj.user_id)},
                "links": {
                    "self": f"{_resource_base_path(self.type)}/{obj.id}/relationships/user",
                    "related": f"{_resource_base_path('user')}/{obj.user_id}",
                },
            }

        # Expose statuses merged with join attributes (created_at, note)
        try:
            statuses_out = []
            for jas in list(getattr(obj, "application_statuses", []) or []):
                st = getattr(jas, "status", None)
                item = {
                    "created_at": _to_primitive(getattr(jas, "created_at", None)),
                    "note": getattr(jas, "note", None),
                }
                if st is not None:
                    item.update(
                        {
                            "id": getattr(st, "id", None),
                            "status": getattr(st, "status", None),
                            "status_type": getattr(st, "status_type", None),
                        }
                    )
                statuses_out.append(item)
            if statuses_out:
                res.setdefault("attributes", {})["statuses"] = statuses_out
        except Exception:
            # Non-fatal; omit statuses on error
            pass

        return res

    def get_related(self, obj, rel_name):
        if rel_name == "user" and hasattr(obj, "user_id") and obj.user_id:
            User = get_user_model()
            try:
                user = User.objects.get(id=obj.user_id)
                return "user", [user]
            except User.DoesNotExist:
                return "user", []
        return super().get_related(obj, rel_name)


class JobPostSerializer(BaseSASerializer):
    type = "job-post"
    model = JobPost
    attributes = [
        "description",
        "title",
        "posted_date",
        "extraction_date",
        "created_at",
        "link",
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
            "type": "job-application",
            "uselist": True,
        },
        "summaries": {"attr": "summaries", "type": "summary", "uselist": True},
    }
    relationship_fks = {"company": "company_id"}

    def parse_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        out = super().parse_payload(payload)

        # Remove created_at to prevent user from overwriting system timestamps
        out.pop("created_at", None)

        # Parse and validate datetime fields
        if "posted_date" in out:
            parsed_dt = _parse_datetime(out["posted_date"])
            if parsed_dt is None and out["posted_date"]:
                raise ValueError("Invalid posted_date")
            out["posted_date"] = parsed_dt

        if "extraction_date" in out:
            parsed_dt = _parse_datetime(out["extraction_date"])
            if parsed_dt is None and out["extraction_date"]:
                raise ValueError("Invalid extraction_date")
            out["extraction_date"] = parsed_dt

        return out


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
        "applications": {
            "attr": "applications",
            "type": "job-application",
            "uselist": True,
        },
    }


class CoverLetterSerializer(BaseSASerializer):
    type = "cover-letter"
    model = CoverLetter
    attributes = ["content", "created_at"]
    relationships = {
        "user": {"attr": "user", "type": "user", "uselist": False},
        "resume": {"attr": "resume", "type": "resume", "uselist": False},
        "job-post": {"attr": "job_post", "type": "job-post", "uselist": False},
        "application": {
            "attr": "application",
            "type": "job-application",
            "uselist": False,
        },
    }
    relationship_fks = {
        "user": "user_id",
        "resume": "resume_id",
        "job-post": "job_post_id",
    }

    def to_resource(self, obj):
        res = super().to_resource(obj)
        # Ensure user relationship linkage points to Django user
        if hasattr(obj, "user_id") and obj.user_id:
            res.setdefault("relationships", {})["user"] = {
                "data": {"type": "user", "id": str(obj.user_id)},
                "links": {
                    "self": f"{_resource_base_path(self.type)}/{obj.id}/relationships/user",
                    "related": f"{_resource_base_path('user')}/{obj.user_id}",
                },
            }

        # Add company relationship via job_post fallback
        company_id = None
        if hasattr(obj, "company_id") and obj.company_id:
            company_id = obj.company_id
        elif (
            hasattr(obj, "job_post")
            and obj.job_post
            and hasattr(obj.job_post, "company_id")
            and obj.job_post.company_id
        ):
            company_id = obj.job_post.company_id

        if company_id:
            res.setdefault("relationships", {})["company"] = {
                "data": {"type": "company", "id": str(company_id)},
                "links": {
                    "self": f"{_resource_base_path(self.type)}/{obj.id}/relationships/company",
                    "related": f"{_resource_base_path('company')}/{company_id}",
                },
            }
        else:
            # Include null relationship for consistency
            res.setdefault("relationships", {})["company"] = {
                "data": None,
                "links": {
                    "self": f"{_resource_base_path(self.type)}/{obj.id}/relationships/company",
                },
            }

        return res

    def get_related(self, obj, rel_name):
        if rel_name == "user" and hasattr(obj, "user_id") and obj.user_id:
            User = get_user_model()
            try:
                user = User.objects.get(id=obj.user_id)
                return "user", [user]
            except User.DoesNotExist:
                return "user", []
        return super().get_related(obj, rel_name)


class ApplicationSerializer(BaseSASerializer):
    type = "job-application"
    model = Application
    attributes = ["applied_at", "status", "tracking_url", "notes"]
    relationships = {
        "user": {"attr": "user", "type": "user", "uselist": False},
        "job-post": {"attr": "job_post", "type": "job-post", "uselist": False},
        "resume": {"attr": "resume", "type": "resume", "uselist": False},
        "company": {"attr": "company", "type": "company", "uselist": False},
        "cover-letter": {
            "attr": "cover_letter",
            "type": "cover-letter",
            "uselist": False,
        },
        "company": {"attr": "company", "type": "company", "uselist": False},
        "questions": {"attr": "questions", "type": "question", "uselist": True},
    }
    relationship_fks = {
        "user": "user_id",
        "users": "user_id",
        "job-post": "job_post_id",
        "job_post": "job_post_id",
        "job-posts": "job_post_id",
        "resume": "resume_id",
        "resumes": "resume_id",
        "company": "company_id",
        "companies": "company_id",
        "cover-letter": "cover_letter_id",
        "cover_letter": "cover_letter_id",
        "cover-letters": "cover_letter_id",
    }

    def accepted_types(self):
        return {"application", "applications", "job-application", "job-applications"}

    def to_resource(self, obj):
        res = super().to_resource(obj)
        # Ensure user relationship linkage points to Django user
        if hasattr(obj, "user_id") and obj.user_id:
            res.setdefault("relationships", {})["user"] = {
                "data": {"type": "user", "id": str(obj.user_id)},
                "links": {
                    "self": f"{_resource_base_path(self.type)}/{obj.id}/relationships/user",
                    "related": f"{_resource_base_path('user')}/{obj.user_id}",
                },
            }
        return res

    def get_related(self, obj, rel_name):
        if rel_name == "user" and hasattr(obj, "user_id") and obj.user_id:
            User = get_user_model()
            try:
                user = User.objects.get(id=obj.user_id)
                return "user", [user]
            except User.DoesNotExist:
                return "user", []
        return super().get_related(obj, rel_name)

    def parse_payload(self, payload):
        out = super().parse_payload(payload)
        if "applied_at" in out:
            parsed_dt = _parse_datetime(out["applied_at"])
            if parsed_dt is None and out["applied_at"]:
                raise ValueError("Invalid applied_at")
            out["applied_at"] = parsed_dt
        return out


class SummarySerializer(BaseSASerializer):
    type = "summary"
    model = Summary
    attributes = ["content"]
    relationships = {
        "user": {"attr": "user", "type": "user", "uselist": False},
        "job-post": {"attr": "job_post", "type": "job-post", "uselist": False},
    }
    relationship_fks = {
        "user": "user_id",
        "job-post": "job_post_id",
    }

    def to_resource(self, obj):
        res = super().to_resource(obj)
        # Ensure user relationship linkage points to Django user
        if hasattr(obj, "user_id") and obj.user_id:
            res.setdefault("relationships", {})["user"] = {
                "data": {"type": "user", "id": str(obj.user_id)},
                "links": {
                    "self": f"{_resource_base_path(self.type)}/{obj.id}/relationships/user",
                    "related": f"{_resource_base_path('user')}/{obj.user_id}",
                },
            }
        # If included under a resume, inject per-link 'active' from resume_summary
        try:
            ctx = getattr(self, "_parent_context", None)
            if ctx and ctx.get("parent_type") == "resume":
                resume_id = ctx.get("parent_id")
                if resume_id:
                    session = self.model.get_session()
                    link = (
                        session.query(ResumeSummaries)
                        .filter_by(resume_id=int(resume_id), summary_id=obj.id)
                        .first()
                    )
                    if link and hasattr(link, "active"):
                        res.setdefault("attributes", {})["active"] = bool(link.active)
        except Exception:
            pass
        return res

    def get_related(self, obj, rel_name):
        if rel_name == "user" and hasattr(obj, "user_id") and obj.user_id:
            User = get_user_model()
            try:
                user = User.objects.get(id=obj.user_id)
                return "user", [user]
            except User.DoesNotExist:
                return "user", []
        return super().get_related(obj, rel_name)


class ExperienceSerializer(BaseSASerializer):
    type = "experience"
    model = Experience
    attributes = ["title", "start_date", "end_date", "location", "content"]
    relationships = {
        "resumes": {"attr": "resumes", "type": "resume", "uselist": True},
        "company": {"attr": "company", "type": "company", "uselist": False},
        "descriptions": {
            "attr": "descriptions",
            "type": "description",
            "uselist": True,
        },
    }
    relationship_fks = {"company": "company_id"}

    def to_resource(self, obj):
        res = super().to_resource(obj)
        # Convenience link to related descriptions (non-relationships URL)
        res.setdefault("links", {})[
            "descriptions"
        ] = f"{_resource_base_path(self.type)}/{obj.id}/descriptions"
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

        def _dp(val):
            if val is None or val == "":
                return None
            if isinstance(val, (datetime, date)):
                return val.date() if isinstance(val, datetime) else val
            try:
                dt = dateparser.parse(str(val))
                return dt.date() if dt else None
            except Exception:
                return None

        if "start_date" in out:
            out["start_date"] = _dp(out["start_date"])
        if "end_date" in out:
            out["end_date"] = _dp(out["end_date"])
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
                        .filter_by(
                            experience_id=int(experience_id), description_id=obj.id
                        )
                        .first()
                    )
                    if link and hasattr(link, "order"):
                        res.setdefault("attributes", {})["order"] = link.order
        except Exception:
            # Non-fatal; omit 'order' if unavailable
            pass
        return res


class SkillSerializer(BaseSASerializer):
    type = "skill"
    model = Skill
    attributes = ["text", "skill_type"]
    relationships = {
        "resumes": {"attr": "resumes", "type": "resume", "uselist": True},
    }

    def to_resource(self, obj):
        res = super().to_resource(obj)
        # If included under a resume, expose per-link 'active' from resume_skill join
        try:
            ctx = getattr(self, "_parent_context", None)
            if ctx and ctx.get("parent_type") == "resume":
                resume_id = ctx.get("parent_id")
                if resume_id:
                    session = self.model.get_session()
                    link = (
                        session.query(ResumeSkill)
                        .filter_by(resume_id=int(resume_id), skill_id=obj.id)
                        .first()
                    )
                    if link and hasattr(link, "active"):
                        res.setdefault("attributes", {})["active"] = bool(link.active)
        except Exception:
            pass
        return res


class StatusSerializer(BaseSASerializer):
    type = "status"
    model = Status
    attributes = ["status", "status_type"]


class JobApplicationStatusSerializer(BaseSASerializer):
    type = "job-application-status"
    model = JobApplicationStatus
    attributes = ["created_at", "note"]
    relationships = {
        "application": {
            "attr": "application",
            "type": "job-application",
            "uselist": False,
        },
        "status": {"attr": "status", "type": "status", "uselist": False},
        "company": {"attr": "name", "type": "company", "userlist": False},
    }
    relationship_fks = {
        "application": "application_id",
        "status": "status_id",
    }


class AnswerSerializer(BaseSASerializer):
    type = "answer"
    model = Answer
    attributes = ["content", "created_at"]
    relationships = {
        "question": {"attr": "question", "type": "question", "uselist": False},
    }
    relationship_fks = {"question": "question_id"}


class QuestionSerializer(BaseSASerializer):
    type = "question"
    model = Question
    attributes = ["content", "created_at"]
    relationships = {
        "application": {
            "attr": "application",
            "type": "job-application",
            "uselist": False,
        },
        "company": {"attr": "company", "type": "company", "uselist": False},
        "user": {"attr": "user", "type": "user", "uselist": False},
        "answers": {"attr": "answers", "type": "answer", "uselist": True},
    }
    relationship_fks = {
        # Accept multiple relationship keys for application
        "application": "application_id",
        "job-application": "application_id",
        "job-applications": "application_id",
        "job_application": "application_id",
        "job_applications": "application_id",
        "company": "company_id",
        "user": "created_by_id",
    }

    def to_resource(self, obj):
        res = super().to_resource(obj)

        # Backward-compatible: expose latest answer content as an attribute
        try:
            latest_content = None
            answers = list(getattr(obj, "answers", []) or [])
            if answers:
                try:
                    latest = max(
                        answers,
                        key=lambda a: (
                            getattr(a, "created_at", None) or datetime.min,
                            getattr(a, "id", 0) or 0,
                        ),
                    )
                except Exception:
                    latest = answers[-1]
                latest_content = getattr(latest, "content", None)

            # Fallback to legacy column if present and no child answers yet
            if not latest_content:
                legacy = getattr(obj, "answer", None)
                if legacy:
                    latest_content = legacy

            if latest_content is not None:
                res.setdefault("attributes", {})["answer"] = latest_content
        except Exception:
            # Non-fatal; omit 'answer' on error
            pass

        # Ensure user relationship linkage points to Django user
        if hasattr(obj, "created_by_id") and obj.created_by_id:
            res.setdefault("relationships", {})["user"] = {
                "data": {"type": "user", "id": str(obj.created_by_id)},
                "links": {
                    "self": f"{_resource_base_path(self.type)}/{obj.id}/relationships/user",
                    "related": f"{_resource_base_path('user')}/{obj.created_by_id}",
                },
            }
        return res

    def get_related(self, obj, rel_name):
        if rel_name == "user" and hasattr(obj, "created_by_id") and obj.created_by_id:
            User = get_user_model()
            try:
                user = User.objects.get(id=obj.created_by_id)
                return "user", [user]
            except User.DoesNotExist:
                return "user", []
        return super().get_related(obj, rel_name)

    def parse_payload(self, payload):
        out = super().parse_payload(payload)
        # Accept 'content' as an alias for 'question' in attributes
        try:
            data = payload.get("data") if isinstance(payload, dict) else None
            attrs_in = (data.get("attributes") or {}) if isinstance(data, dict) else {}
        except Exception:
            pass
        return out


TYPE_TO_SERIALIZER = {
    "user": DjangoUserSerializer,
    "resume": ResumeSerializer,
    "score": ScoreSerializer,
    "job-post": JobPostSerializer,
    "scrape": ScrapeSerializer,
    "company": CompanySerializer,
    "cover-letter": CoverLetterSerializer,
    "application": ApplicationSerializer,
    "job-application": ApplicationSerializer,
    "job-applications": ApplicationSerializer,
    "summary": SummarySerializer,
    "experience": ExperienceSerializer,
    "education": EducationSerializer,
    "certification": CertificationSerializer,
    "description": DescriptionSerializer,
    "skill": SkillSerializer,
    "status": StatusSerializer,
    "job-application-status": JobApplicationStatusSerializer,
    "question": QuestionSerializer,
    "answer": AnswerSerializer,
}
