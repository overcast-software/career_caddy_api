from datetime import date, datetime, timezone
from typing import Any, Dict, List

import dateparser
from django.contrib.auth import get_user_model
from django.db.models import Q
from rest_framework import serializers

from job_hunting.models import (
    Status, Skill, Description, Certification, Education, Summary,
    Company, ApiKey, Question, JobPost,
    Answer, JobApplication, CoverLetter, Experience, Resume, Score, Scrape,
    ExperienceDescription, ResumeSkill, ResumeSummary, JobApplicationStatus,
    Project, ResumeProject, ResumeExperience, ResumeEducation, ResumeCertification,
    AiUsage, Waitlist, Invitation, ScrapeProfile,
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
        pass
    try:
        dt = dateparser.parse(str(val))
        return dt.date() if dt else None
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


class BaseSerializer:
    type: str
    model: Any
    attributes: List[str] = []
    # Subset of `attributes` that the serializer outputs in to_resource()
    # but refuses to accept on input. Use for derived/computed properties
    # (e.g. Scrape.latest_status_note) that have no setter — without this
    # filter, _upsert calls setattr(obj, name, val) and crashes with
    # "property has no setter". Frontend round-trip saves still work
    # because the field is silently dropped from the inbound payload.
    read_only_attributes: List[str] = []
    relationships: Dict[str, Dict[str, Any]] = {}
    relationship_fks: Dict[str, str] = {}
    # Subclasses can declare which attributes to expose when slim=True.
    # Defaults to ["name"] — override per serializer as needed.
    slim_attributes: List[str] = ["name"]
    slim: bool = False
    # Set to a FK field name (e.g. "user_id", "created_by_id") to auto-inject
    # user relationship linkage in to_resource() and resolve in get_related().
    user_fk: str = ""
    # List of relationship names whose data linkage arrays should be
    # auto-populated in to_resource() (for Ember Data sideload resolution).
    linked_relationships: List[str] = []

    def set_parent_context(self, parent_type: str, parent_id: int, rel_name: str):
        self._parent_context = {
            "parent_type": parent_type,
            "parent_id": parent_id,
            "rel_name": rel_name,
        }

    def accepted_types(self):
        return {self.type, _pluralize_type(self.type)}

    def to_slim_resource(self, obj) -> Dict[str, Any]:
        """Return a minimal representation suitable for dropdown lists."""
        return {
            "type": self.type,
            "id": str(obj.id),
            "attributes": {
                k: _to_primitive(getattr(obj, k, None)) for k in self.slim_attributes
            },
        }

    def to_resource(self, obj) -> Dict[str, Any]:
        if self.slim:
            return self.to_slim_resource(obj)
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
                
                # Safely get target, catching DoesNotExist errors
                target = None
                try:
                    target = getattr(obj, rel_attr, None)
                except Exception:
                    # Relationship doesn't exist or target is missing - skip it
                    target = None
                
                if uselist:
                    # Map relationship name to URL segment for special cases
                    rel_segment = rel_name
                    links = {
                        "self": f"{_resource_base_path(self.type)}/{obj.id}/relationships/{rel_name}",
                        "related": f"{_resource_base_path(self.type)}/{obj.id}/{rel_segment}",
                    }
                    # Per JSON:API spec: only emit `links` for to-many relationships.
                    # Emitting `data: []` falsely asserts zero items; full objects
                    # come through `included` when explicitly requested.
                    rel_out[rel_name] = {"links": links}
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
        # Auto-inject user relationship linkage when user_fk is declared
        if self.user_fk:
            fk_value = getattr(obj, self.user_fk, None)
            if fk_value:
                res.setdefault("relationships", {})["user"] = {
                    "data": {"type": "user", "id": str(fk_value)},
                    "links": {
                        "self": f"{_resource_base_path(self.type)}/{obj.id}/relationships/user",
                        "related": f"{_resource_base_path('user')}/{fk_value}",
                    },
                }
        # Auto-populate data linkage for declared linked_relationships
        if not self.slim and self.linked_relationships:
            for rel_name in self.linked_relationships:
                rel_cfg = self.relationships.get(rel_name)
                if not rel_cfg:
                    continue
                rel_type = rel_cfg["type"]
                try:
                    _, items = self.get_related(obj, rel_name)
                    linkage = [{"type": rel_type, "id": str(item.id)} for item in items]
                except Exception:
                    linkage = []
                existing_links = res.get("relationships", {}).get(rel_name, {}).get("links", {
                    "self": f"{_resource_base_path(self.type)}/{obj.id}/relationships/{rel_name}",
                    "related": f"{_resource_base_path(self.type)}/{obj.id}/{rel_name}",
                })
                res.setdefault("relationships", {})[rel_name] = {
                    "data": linkage,
                    "links": existing_links,
                }
        return res

    def get_related(self, obj, rel_name):
        # Auto-resolve user relationship when user_fk is declared
        if rel_name == "user" and self.user_fk:
            fk_value = getattr(obj, self.user_fk, None)
            if fk_value:
                User = get_user_model()
                try:
                    return "user", [User.objects.get(id=fk_value)]
                except User.DoesNotExist:
                    return "user", []
            return "user", []
        cfg = self.relationships.get(rel_name)
        if not cfg:
            return None, []
        attr = cfg["attr"]
        rel_type = cfg["type"]
        uselist = cfg.get("uselist", True)
        target = getattr(obj, attr, None)
        if target is None:
            return rel_type, []
        # Handle Django Managers (reverse FK / M2M) by calling .all()
        if hasattr(target, "all"):
            target = target.all()
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

        read_only = set(self.read_only_attributes)
        for k in self.attributes:
            if k in attrs_in and k not in read_only:
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
    model = get_user_model()

    def accepted_types(self):
        return {self.type, _pluralize_type(self.type)}

    def to_resource(self, obj) -> Dict[str, Any]:
        # Fetch profile fields from Django Profile
        phone = ""
        is_guest = False
        linkedin = ""
        github = ""
        address = ""
        links = []
        onboarding = None
        auto_score = False
        try:
            from job_hunting.models import Profile
            prof = Profile.objects.filter(user_id=obj.id).first()
            if prof:
                phone = prof.phone or ""
                is_guest = bool(getattr(prof, "is_guest", False))
                linkedin = prof.linkedin or ""
                github = prof.github or ""
                address = prof.address or ""
                links = prof.links if prof.links is not None else []
                onboarding = prof.resolved_onboarding()
                auto_score = bool(getattr(prof, "auto_score", False))
        except Exception:
            phone = ""
        if onboarding is None:
            from job_hunting.models import Profile
            onboarding = Profile.default_onboarding()
        # Derive `profile_basics` from User fields so fresh users aren't told
        # to fill in their name when they already did at signup. Kept as a
        # read-time derivation; reconcile remains authoritative for the rest.
        onboarding["profile_basics"] = bool(
            obj.first_name and obj.last_name and (obj.email or "")
        )

        res = {
            "type": self.type,
            "id": str(obj.id),
            "attributes": {
                "username": obj.username,
                "email": obj.email or "",
                "first_name": obj.first_name or "",
                "last_name": obj.last_name or "",
                "phone": phone or "",
                "is_guest": is_guest,
                "is_staff": bool(obj.is_staff),
                "is_active": bool(obj.is_active),
                "linkedin": linkedin,
                "github": github,
                "address": address,
                "links": links,
                "onboarding": onboarding,
                "auto_score": auto_score,
            },
        }
        res["links"] = {"self": f"{_resource_base_path(self.type)}/{obj.id}"}

        # Build relationships with resource linkage so clients can resolve sideloaded records.
        rel_defs = [
            ("resumes", "resume", "resumes"),
            ("scores", "score", "scores"),
            ("cover-letters", "cover-letter", "cover-letters"),
            ("job-applications", "job-application", "job-applications"),
            ("summaries", "summary", "summaries"),
        ]
        relationships = {}
        for rel_name, rel_type, url_segment in rel_defs:
            try:
                _, items = self.get_related(obj, rel_name)
                linkage_data = [{"type": rel_type, "id": str(item.id)} for item in items]
            except Exception:
                linkage_data = []
            relationships[rel_name] = {
                "data": linkage_data,
                "links": {
                    "self": f"{_resource_base_path(self.type)}/{obj.id}/relationships/{rel_name}",
                    "related": f"{_resource_base_path(self.type)}/{obj.id}/{url_segment}",
                },
            }
        res["relationships"] = relationships
        return res

    def get_related(self, obj, rel_name):
        if rel_name == "resumes":
            return "resume", list(Resume.objects.filter(user_id=obj.id))
        elif rel_name == "scores":
            return "score", list(Score.objects.filter(user_id=obj.id))
        elif rel_name == "cover-letters":
            return "cover-letter", list(CoverLetter.objects.filter(user_id=obj.id))
        elif rel_name == "job-applications":
            return "job-application", list(JobApplication.objects.filter(user_id=obj.id))
        elif rel_name == "summaries":
            return "summary", list(Summary.objects.filter(user_id=obj.id))
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
        for k in [
            "username", "email", "first_name", "last_name", "password",
            "phone", "linkedin", "github", "address", "links",
            "is_staff", "is_active", "onboarding", "auto_score",
        ]:
            if k in attrs_in:
                out[k] = attrs_in[k]
        # Accept hyphenated variants from JSON:API clients
        for hyphen, snake in [
            ("is-staff", "is_staff"),
            ("is-active", "is_active"),
            ("first-name", "first_name"),
            ("last-name", "last_name"),
        ]:
            if hyphen in attrs_in and snake not in out:
                out[snake] = attrs_in[hyphen]
        return out


class ResumeSerializer(BaseSerializer):
    type = "resume"
    model = Resume
    attributes = ["file_path", "title", "name", "notes", "user_id", "favorite", "status"]
    slim_attributes = ["name", "title", "notes", "favorite"]
    user_fk = "user_id"
    relationships = {
        "user": {"attr": "user", "type": "user", "uselist": False},
        "scores": {"attr": "scores", "type": "score", "uselist": True},
        "cover-letters": {
            "attr": "cover_letters",
            "type": "cover-letter",
            "uselist": True,
        },
        "job-applications": {
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
        "projects": {"attr": "projects", "type": "project", "uselist": True},
    }
    relationship_fks = {"user": "user_id"}
    linked_relationships = [
        "summaries", "certifications", "educations",
        "experiences", "skills", "projects",
    ]

    def to_resource(self, obj):
        res = super().to_resource(obj)
        if self.slim:
            res["meta"] = self._build_counts(obj)
            return res
        # Convenience attribute: active summary content
        try:
            res.setdefault("attributes", {})["summary"] = obj.active_summary_content()
        except Exception:
            pass
        return res

    def _build_counts(self, obj):
        from job_hunting.models import ResumeExperience, ResumeSkill
        from job_hunting.models.job_application import JobApplication
        from job_hunting.models.score import Score

        rid = obj.id
        return {
            "job_application_count": JobApplication.objects.filter(resume_id=rid).count(),
            "score_count": Score.objects.filter(resume_id=rid).count(),
            "experience_count": ResumeExperience.objects.filter(resume_id=rid).count(),
            "skill_count": ResumeSkill.objects.filter(resume_id=rid).count(),
        }

    def get_related(self, obj, rel_name):
        if rel_name == "experiences":
            exp_ids = list(
                ResumeExperience.objects.filter(resume_id=obj.id)
                .order_by("order")
                .values_list("experience_id", flat=True)
            )
            by_id = {e.id: e for e in Experience.objects.filter(pk__in=exp_ids)}
            return "experience", [by_id[i] for i in exp_ids if i in by_id]
        elif rel_name == "educations":
            edu_ids = list(
                ResumeEducation.objects.filter(resume_id=obj.id)
                .values_list("education_id", flat=True)
            )
            return "education", list(Education.objects.filter(pk__in=edu_ids))
        elif rel_name == "skills":
            skill_ids = list(
                ResumeSkill.objects.filter(resume_id=obj.id)
                .values_list("skill_id", flat=True)
            )
            return "skill", list(Skill.objects.filter(pk__in=skill_ids))
        elif rel_name == "summaries":
            summary_ids = list(
                ResumeSummary.objects.filter(resume_id=obj.id)
                .values_list("summary_id", flat=True)
            )
            return "summary", list(Summary.objects.filter(pk__in=summary_ids))
        elif rel_name == "projects":
            project_ids = list(
                ResumeProject.objects.filter(resume_id=obj.id)
                .order_by("order")
                .values_list("project_id", flat=True)
            )
            by_id = {p.id: p for p in Project.objects.filter(pk__in=project_ids)}
            return "project", [by_id[i] for i in project_ids if i in by_id]
        elif rel_name == "certifications":
            cert_ids = list(
                ResumeCertification.objects.filter(resume_id=obj.id)
                .values_list("certification_id", flat=True)
            )
            return "certification", list(Certification.objects.filter(pk__in=cert_ids))
        return super().get_related(obj, rel_name)


class ScoreSerializer(BaseSerializer):
    type = "score"
    model = Score
    attributes = ["score", "status", "explanation", "created_at"]
    user_fk = "user_id"
    relationships = {
        "resume": {"attr": "resume", "type": "resume", "uselist": False},
        "job-post": {"attr": "job_post", "type": "job-post", "uselist": False},
        "user": {"attr": "user", "type": "user", "uselist": False},
        "company": {"attr": "company", "type": "company", "uselist": False},
    }
    relationship_fks = {
        "resume": "resume_id",
        "job-post": "job_post_id",
        "user": "user_id",
    }

    def to_resource(self, obj):
        res = super().to_resource(obj)
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


class JobPostSerializer(BaseSerializer):
    type = "job-post"
    model = JobPost
    attributes = [
        "description",
        "title",
        "posted_date",
        "extraction_date",
        "created_at",
        "link",
        "salary_min",
        "salary_max",
        "location",
        "remote",
        "top_score",
        "active_application_status",
        "source",
        "apply_url",
        "apply_url_status",
        "apply_url_resolved_at",
        "duplicate_of_id",
    ]
    relationships = {
        "company": {"attr": "company", "type": "company", "uselist": False},
        "cover-letters": {
            "attr": "cover_letters",
            "type": "cover-letter",
            "uselist": True,
        },
        "job-applications": {
            "attr": "applications",
            "type": "job-application",
            "uselist": True,
        },
        "summaries": {"attr": "summaries", "type": "summary", "uselist": True},
        "questions": {"attr": "questions", "type": "question", "uselist": True},
        "scores": {"attr": "scores", "type": "score", "uselist": True},
        "scrapes": {"attr": "scrapes", "type": "scrape", "uselist": True},
        "top-score": {"attr": "top_score_record", "type": "score", "uselist": False},
    }
    relationship_fks = {"company": "company_id"}
    linked_relationships = [
        "scores", "questions", "summaries",
        "cover-letters", "job-applications",
    ]

    def get_related(self, obj, rel_name):
        # `Summary.job_post_id` is a plain IntegerField (not a ForeignKey), so
        # there is no `obj.summaries` reverse accessor — query manually and
        # scope to the requesting user when we have one.
        if rel_name == "summaries":
            request = getattr(self, "request", None)
            user_id = (
                getattr(getattr(request, "user", None), "id", None)
                if request else None
            )
            qs = Summary.objects.filter(job_post_id=obj.id)
            if user_id:
                qs = qs.filter(user_id=user_id)
            return "summary", list(qs)
        return super().get_related(obj, rel_name)


class ScrapeSerializer(BaseSerializer):
    type = "scrape"
    model = Scrape
    attributes = [
        "url",
        "css_selectors",
        "job_content",
        "external_link",
        "parse_method",
        "scraped_at",
        "status",
        "html",
        "latest_status_note",
        "apply_url",
        "apply_url_status",
    ]
    # latest_status_note is a derived @property on Scrape with no setter —
    # output it but reject it on PATCH so frontend round-trips don't 500
    # with "property has no setter".
    read_only_attributes = ["latest_status_note"]
    relationships = {
        "job-post": {"attr": "job_post", "type": "job-post", "uselist": False},
        "company": {"attr": "company", "type": "company", "uselist": False},
        "scrape-statuses": {"attr": "scrape_statuses", "type": "scrape-status", "uselist": True},
    }
    relationship_fks = {"job-post": "job_post_id", "company": "company_id"}


class ScrapeStatusSerializer(BaseSerializer):
    type = "scrape-status"
    attributes = ["logged_at", "note", "created_at", "graph_node", "graph_payload"]
    relationships = {
        "scrape": {"attr": "scrape", "type": "scrape", "uselist": False},
        "status": {"attr": "status", "type": "status", "uselist": False},
    }
    relationship_fks = {"scrape": "scrape_id", "status": "status_id"}


class CompanySerializer(BaseSerializer):
    type = "company"
    model = Company
    attributes = ["name", "display_name", "notes"]
    relationships = {
        "job-posts": {"attr": "job_posts", "type": "job-post", "uselist": True},
        "job-applications": {
            "attr": "job_applications",
            "type": "job-application",
            "uselist": True,
        },
        "scrapes": {"attr": "scrapes", "type": "scrape", "uselist": True},
        "questions": {"attr": "questions", "type": "question", "uselist": True},
        "scores": {"attr": "scores", "type": "score", "uselist": True},
    }

    def get_related(self, obj, rel_name):
        request = getattr(self, "request", None)
        user_id = getattr(getattr(request, "user", None), "id", None) if request else None
        if rel_name == "job-posts":
            qs = JobPost.objects.filter(company_id=obj.id)
            if user_id:
                qs = qs.filter(
                    Q(created_by_id=user_id) |
                    Q(applications__user_id=user_id) |
                    Q(scores__user_id=user_id)
                ).distinct()
            return "job-post", list(qs)
        elif rel_name == "job-applications":
            qs = JobApplication.objects.filter(company_id=obj.id)
            if user_id:
                qs = qs.filter(user_id=user_id)
            return "job-application", list(qs)
        return None, []


class CoverLetterSerializer(BaseSerializer):
    type = "cover-letter"
    model = CoverLetter
    attributes = ["content", "created_at", "favorite", "status"]
    user_fk = "user_id"
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


class JobApplicationSerializer(BaseSerializer):
    type = "job-application"
    model = JobApplication
    attributes = ["applied_at", "status", "tracking_url", "notes"]
    user_fk = "user_id"
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
        "questions": {"attr": "questions", "type": "question", "uselist": True},
        "application-statuses": {
            "attr": "application_statuses",
            "type": "job-application-status",
            "uselist": True,
        },
    }
    # Without this, the to-many relationships emit only `links` (per JSON:API
    # spec) and Ember Data can't populate the hasMany from `included` —
    # <Applications::StatusLog> reads an empty applicationStatuses and renders
    # "No history yet" even when the DB has rows. Forcing data-linkage means
    # the frontend must also `?include=application-statuses` so the sideload
    # actually ships the records — done in routes/job-applications/show.js.
    linked_relationships = ["application-statuses"]
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

    def parse_payload(self, payload):
        out = super().parse_payload(payload)
        if "applied_at" in out:
            parsed_dt = _parse_datetime(out["applied_at"])
            if parsed_dt is None and out["applied_at"]:
                raise ValueError("Invalid applied_at")
            out["applied_at"] = parsed_dt
        return out


class SummarySerializer(BaseSerializer):
    type = "summary"
    model = Summary
    attributes = ["content", "status"]
    user_fk = "user_id"
    relationships = {
        "user": {"attr": "user", "type": "user", "uselist": False},
        "job-post": {"attr": "job_post_id", "type": "job-post", "uselist": False},
    }
    relationship_fks = {
        "user": "user_id",
        "job-post": "job_post_id",
    }

    def to_resource(self, obj):
        res = super().to_resource(obj)
        # If included under a resume, inject per-link 'active' from resume_summary
        try:
            ctx = getattr(self, "_parent_context", None)
            if ctx and ctx.get("parent_type") == "resume":
                resume_id = ctx.get("parent_id")
                if resume_id:
                    link = ResumeSummary.objects.filter(
                        resume_id=int(resume_id), summary_id=obj.id
                    ).first()
                    if link and hasattr(link, "active"):
                        res.setdefault("attributes", {})["active"] = bool(link.active)
        except Exception:
            pass
        return res


class ExperienceSerializer(BaseSerializer):
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
    linked_relationships = ["descriptions"]

    def _resume_id_for(self, obj):
        """Which resume this experience renders under, for order lookup."""
        ctx = getattr(self, "_parent_context", None)
        if ctx and ctx.get("parent_type") == "resume":
            return ctx.get("parent_id")
        try:
            if getattr(obj, "resumes", None):
                return obj.resumes[0].id
        except Exception:
            pass
        return None

    def to_resource(self, obj):
        res = super().to_resource(obj)
        # Convenience link to related descriptions (non-relationships URL)
        res.setdefault("links", {})[
            "descriptions"
        ] = f"{_resource_base_path(self.type)}/{obj.id}/descriptions"
        rid = self._resume_id_for(obj)
        if rid is not None:
            res.setdefault("attributes", {})["resume_id"] = rid
            row = ResumeExperience.objects.filter(
                resume_id=rid, experience_id=obj.id
            ).first()
            if row is not None:
                res.setdefault("attributes", {})["order"] = row.order
        return res

    def parse_payload(self, payload):
        out = super().parse_payload(payload)
        if "start_date" in out:
            out["start_date"] = _parse_date(out["start_date"])
        if "end_date" in out:
            out["end_date"] = _parse_date(out["end_date"])
        return out


class EducationSerializer(BaseSerializer):
    type = "education"
    model = Education
    attributes = ["degree", "issue_date", "institution", "major", "minor"]

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


class CertificationSerializer(BaseSerializer):
    type = "certification"
    model = Certification
    attributes = ["issuer", "title", "issue_date", "content"]

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


class DescriptionSerializer(BaseSerializer):
    type = "description"
    model = Description
    attributes = ["content"]

    def to_resource(self, obj):
        res = super().to_resource(obj)
        # If included under an experience, inject per-link 'order' from the join table
        try:
            ctx = getattr(self, "_parent_context", None)
            if ctx and ctx.get("parent_type") == "experience":
                experience_id = ctx.get("parent_id")
                if experience_id:
                    link = ExperienceDescription.objects.filter(
                        experience_id=int(experience_id), description_id=obj.id
                    ).first()
                    if link and hasattr(link, "order"):
                        res.setdefault("attributes", {})["order"] = link.order
        except Exception:
            # Non-fatal; omit 'order' if unavailable
            pass
        return res


class SkillSerializer(BaseSerializer):
    type = "skill"
    model = Skill
    attributes = ["text", "skill_type"]

    def to_resource(self, obj):
        res = super().to_resource(obj)
        # If included under a resume, expose per-link 'active' from resume_skill join
        try:
            ctx = getattr(self, "_parent_context", None)
            if ctx and ctx.get("parent_type") == "resume":
                resume_id = ctx.get("parent_id")
                if resume_id:
                    link = ResumeSkill.objects.filter(
                        resume_id=int(resume_id), skill_id=obj.id
                    ).first()
                    if link and hasattr(link, "active"):
                        res.setdefault("attributes", {})["active"] = bool(link.active)
        except Exception:
            pass
        return res


class StatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = Status
        fields = ["id", "status", "status_type", "created_at"]


class JobApplicationStatusSerializer(BaseSerializer):
    type = "job-application-status"
    model = JobApplicationStatus
    attributes = ["created_at", "logged_at", "note"]
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

    def to_resource(self, obj):
        res = super().to_resource(obj)
        # Inline the status label so timeline consumers don't need ?include=status
        try:
            status_obj = obj.status
            if status_obj is not None:
                res["attributes"]["status"] = status_obj.status
                res["attributes"]["status_type"] = status_obj.status_type
        except Exception:
            pass
        return res


class AnswerSerializer(BaseSerializer):
    type = "answer"
    model = Answer
    attributes = ["content", "created_at", "favorite", "status"]
    relationships = {
        "question": {"attr": "question", "type": "question", "uselist": False},
    }
    relationship_fks = {"question": "question_id"}


class ApiKeySerializer(BaseSerializer):
    type = "api-key"
    model = ApiKey
    attributes = [
        "name",
        "key_prefix",
        "is_active",
        "last_used_at",
        "expires_at",
        "created_at",
    ]
    user_fk = "user_id"
    relationships = {
        "user": {"attr": "user", "type": "user", "uselist": False},
    }
    relationship_fks = {"user": "user_id"}

    def to_resource(self, obj):
        res = super().to_resource(obj)
        # Add scopes to attributes
        if hasattr(obj, "get_scopes"):
            res["attributes"]["scopes"] = obj.get_scopes()
        else:
            res["attributes"]["scopes"] = []
        return res


class QuestionSerializer(BaseSerializer):
    type = "question"
    model = Question
    attributes = ["content", "created_at", "favorite"]
    user_fk = "created_by_id"
    relationships = {
        "application": {
            "attr": "application",
            "type": "job-application",
            "uselist": False,
        },
        "company": {"attr": "company", "type": "company", "uselist": False},
        "user": {"attr": "user", "type": "user", "uselist": False},
        "job-post": {"attr": "job_post", "type": "job-post", "uselist": False},
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
        "job-post": "job_post_id",
        "job_post": "job_post_id",
    }

    def to_resource(self, obj):
        res = super().to_resource(obj)

        # Backward-compatible: expose latest answer content as an attribute
        try:
            latest_content = None
            try:
                answers = list(Answer.objects.filter(question_id=obj.id).order_by("created_at"))
            except Exception:
                answers = []
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
        return res

    def parse_payload(self, payload):
        out = super().parse_payload(payload)
        # Accept 'content' as an alias for 'question' in attributes
        return out


class ProjectSerializer(BaseSerializer):
    type = "project"
    model = Project
    attributes = ["title", "description", "start_date", "end_date", "is_active", "created_at", "updated_at"]
    relationships = {
        "user": {"attr": "user", "type": "user", "uselist": False},
        "descriptions": {"attr": "descriptions", "type": "description", "uselist": True},
    }
    relationship_fks = {"user": "user_id"}
    linked_relationships = ["descriptions"]


class AiUsageSerializer(BaseSerializer):
    type = "ai-usage"
    model = AiUsage
    attributes = [
        "agent_name",
        "model_name",
        "trigger",
        "pipeline_run_id",
        "request_tokens",
        "response_tokens",
        "total_tokens",
        "request_count",
        "estimated_cost_usd",
        "created_at",
    ]
    relationships = {
        "user": {"attr": "user", "type": "user", "uselist": False},
    }
    relationship_fks = {"user": "user_id"}


class WaitlistSerializer(BaseSerializer):
    type = "waitlist"
    model = Waitlist
    attributes = ["email", "notes", "created_at", "updated_at"]
    relationships = {}
    relationship_fks = {}


class InvitationSerializer(BaseSerializer):
    type = "invitation"
    model = Invitation
    attributes = ["email", "token", "created_at", "accepted_at", "expires_at"]
    relationships = {
        "created-by": {"attr": "created_by", "type": "user", "uselist": False},
    }
    relationship_fks = {"created-by": "created_by_id"}


class ScrapeProfileSerializer(BaseSerializer):
    type = "scrape-profile"
    model = ScrapeProfile
    attributes = [
        "hostname", "requires_auth", "avg_content_length", "success_rate",
        "css_selectors", "url_rewrites", "apply_resolver_config",
        "extraction_hints", "page_structure",
        "last_success_at", "scrape_count", "failure_count", "tier0_miss_count",
        "preferred_tier", "enabled", "created_at", "updated_at",
    ]
    relationships = {}
    relationship_fks = {}


TYPE_TO_SERIALIZER = {
    "user": DjangoUserSerializer,
    "api-key": ApiKeySerializer,
    "resume": ResumeSerializer,
    "score": ScoreSerializer,
    "job-post": JobPostSerializer,
    "scrape": ScrapeSerializer,
    "company": CompanySerializer,
    "cover-letter": CoverLetterSerializer,
    "application": JobApplicationSerializer,
    "job-application": JobApplicationSerializer,
    "job-applications": JobApplicationSerializer,
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
    "project": ProjectSerializer,
    "ai-usage": AiUsageSerializer,
    "waitlist": WaitlistSerializer,
    "invitation": InvitationSerializer,
    "scrape-profile": ScrapeProfileSerializer,
}
