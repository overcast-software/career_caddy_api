from datetime import date, datetime, timezone
from typing import Any, Dict, List

import dateparser
from django.contrib.auth import get_user_model
from rest_framework import serializers

from job_hunting.models import (
    Status, Skill, Description, Certification, Education, Summary,
    Company, ApiKey, Question, JobPost,
    Answer, JobApplication, CoverLetter, Experience, Resume, Score, Scrape,
    ExperienceDescription, ResumeSkill, ResumeSummary, JobApplicationStatus,
    Project, ResumeProject, ResumeExperience, ResumeEducation, ResumeCertification,
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
    # Subclasses can declare which attributes to expose when slim=True.
    # Defaults to ["name"] — override per serializer as needed.
    slim_attributes: List[str] = ["name"]
    slim: bool = False

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
                    rel_segment = (
                        "job-applications" if rel_name == "applications" else rel_name
                    )
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
    model = get_user_model()

    def accepted_types(self):
        return {self.type, _pluralize_type(self.type)}

    def to_resource(self, obj) -> Dict[str, Any]:
        # Fetch phone from Django Profile
        phone = ""
        try:
            from job_hunting.models import Profile
            prof = Profile.objects.filter(user_id=obj.id).first()
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

        # Build relationships with resource linkage so clients can resolve sideloaded records.
        rel_defs = [
            ("resumes", "resume", "resumes"),
            ("scores", "score", "scores"),
            ("cover-letters", "cover-letter", "cover-letters"),
            ("applications", "job-application", "job-applications"),
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
        elif rel_name == "applications":
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
        for k in ["username", "email", "first_name", "last_name", "password", "phone"]:
            if k in attrs_in:
                out[k] = attrs_in[k]
        return out


class ResumeSerializer(BaseSASerializer):
    type = "resume"
    model = Resume
    attributes = ["file_path", "title", "name", "notes", "user_id", "favorite"]
    slim_attributes = ["name", "title"]
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
        "projects": {"attr": "projects", "type": "project", "uselist": True},
    }
    relationship_fks = {"user": "user_id"}

    def to_resource(self, obj):
        res = super().to_resource(obj)
        if self.slim:
            return res
        # Embed active summary content as a convenience attribute
        try:
            res.setdefault("attributes", {})["summary"] = obj.active_summary_content()
        except Exception:
            pass
        # Embed all skills inline — intrinsic to the record, never paginated
        try:
            rs_qs = ResumeSkill.objects.select_related("skill").filter(resume_id=obj.id)
            res["attributes"]["skills"] = [
                {
                    "id": rs.skill.id,
                    "text": rs.skill.text,
                    "skill_type": rs.skill.skill_type,
                    "active": rs.active,
                }
                for rs in rs_qs
            ]
        except Exception:
            res["attributes"]["skills"] = []

        # Embed experiences with their descriptions
        try:
            re_qs = (
                ResumeExperience.objects.select_related("experience", "experience__company")
                .filter(resume_id=obj.id)
                .order_by("order")
            )
            exp_ids = [re.experience_id for re in re_qs]
            # Batch-fetch all descriptions for these experiences
            ed_rows = (
                ExperienceDescription.objects.select_related("description")
                .filter(experience_id__in=exp_ids)
                .order_by("order")
            )
            descs_by_exp = {}
            for ed in ed_rows:
                descs_by_exp.setdefault(ed.experience_id, []).append(
                    {"id": ed.description.id, "content": ed.description.content}
                )
            res["attributes"]["experiences"] = [
                {
                    "id": re.experience.id,
                    "title": re.experience.title,
                    "start_date": _to_primitive(re.experience.start_date),
                    "end_date": _to_primitive(re.experience.end_date),
                    "content": re.experience.content,
                    "location": re.experience.location,
                    "summary": re.experience.summary,
                    "company_id": re.experience.company_id,
                    "company": re.experience.company.name if re.experience.company_id else None,
                    "order": re.order,
                    "descriptions": descs_by_exp.get(re.experience_id, []),
                }
                for re in re_qs
            ]
        except Exception:
            res["attributes"]["experiences"] = []

        # Embed projects
        try:
            rp_qs = (
                ResumeProject.objects.select_related("project")
                .filter(resume_id=obj.id)
                .order_by("order")
            )
            res["attributes"]["projects"] = [
                {
                    "id": rp.project.id,
                    "title": rp.project.title,
                    "description": rp.project.description,
                    "start_date": _to_primitive(rp.project.start_date),
                    "end_date": _to_primitive(rp.project.end_date),
                    "is_active": rp.project.is_active,
                    "order": rp.order,
                }
                for rp in rp_qs
            ]
        except Exception:
            res["attributes"]["projects"] = []

        # Embed educations — join table fields override base model when set
        try:
            red_qs = ResumeEducation.objects.select_related("education").filter(resume_id=obj.id)
            res["attributes"]["educations"] = [
                {
                    "id": red.education.id,
                    "degree": red.degree or red.education.degree,
                    "institution": red.institution or red.education.institution,
                    "issue_date": _to_primitive(red.issue_date or red.education.issue_date),
                    "major": red.education.major,
                    "minor": red.education.minor,
                    "content": red.content,
                }
                for red in red_qs
            ]
        except Exception:
            res["attributes"]["educations"] = []

        # Embed certifications — join table fields override base model when set
        try:
            rc_qs = ResumeCertification.objects.select_related("certification").filter(resume_id=obj.id)
            res["attributes"]["certifications"] = [
                {
                    "id": rc.certification.id,
                    "title": rc.title or rc.certification.title,
                    "issuer": rc.issuer or rc.certification.issuer,
                    "issue_date": _to_primitive(rc.issue_date or rc.certification.issue_date),
                    "content": rc.content or rc.certification.content,
                }
                for rc in rc_qs
            ]
        except Exception:
            res["attributes"]["certifications"] = []

        # Embed summaries
        try:
            rsm_qs = ResumeSummary.objects.select_related("summary").filter(resume_id=obj.id)
            res["attributes"]["summaries"] = [
                {
                    "id": rsm.summary.id,
                    "content": rsm.summary.content,
                    "status": rsm.summary.status,
                    "job_post_id": rsm.summary.job_post_id,
                    "active": rsm.active,
                }
                for rsm in rsm_qs
            ]
        except Exception:
            res["attributes"]["summaries"] = []

        # Ensure user relationship linkage points to Django user
        # Handle both Django and SA Resume models
        user_id = None
        if hasattr(obj, "user_id"):
            user_id = obj.user_id
        
        if user_id:
            # Verify user exists before adding relationship
            try:
                User = get_user_model()
                if User.objects.filter(id=user_id).exists():
                    res.setdefault("relationships", {})["user"] = {
                        "data": {"type": "user", "id": str(user_id)},
                        "links": {
                            "self": f"{_resource_base_path(self.type)}/{obj.id}/relationships/user",
                            "related": f"{_resource_base_path('user')}/{user_id}",
                        },
                    }
            except Exception:
                # User doesn't exist or error checking - skip relationship
                pass

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
        elif rel_name == "experiences":
            exp_ids = list(
                ResumeExperience.objects.filter(resume_id=obj.id)
                .order_by("order")
                .values_list("experience_id", flat=True)
            )
            return "experience", list(Experience.objects.filter(pk__in=exp_ids))
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
            return "project", list(Project.objects.filter(pk__in=project_ids))
        elif rel_name == "certifications":
            cert_ids = list(
                ResumeCertification.objects.filter(resume_id=obj.id)
                .values_list("certification_id", flat=True)
            )
            return "certification", list(Certification.objects.filter(pk__in=cert_ids))
        return super().get_related(obj, rel_name)


class ScoreSerializer(BaseSASerializer):
    type = "score"
    model = Score
    attributes = ["score", "status", "explanation"]
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
        "salary_min",
        "salary_max",
        "location",
        "remote",
        "top_score",
    ]
    relationships = {
        "company": {"attr": "company", "type": "company", "uselist": False},
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
        "questions": {"attr": "questions", "type": "question", "uselist": True},
        "scores": {"attr": "scores", "type": "score", "uselist": True},
        "top-score": {"attr": "top_score_record", "type": "score", "uselist": False},
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
        "status",
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
    attributes = ["name", "display_name", "notes"]
    relationships = {
        "job-posts": {"attr": "job_posts", "type": "job-post", "uselist": True},
        "job-applications": {
            "attr": "job_applications",
            "type": "job-application",
            "uselist": True,
        },
    }

    def get_related(self, obj, rel_name):
        if rel_name == "job-posts":
            return "job-post", list(JobPost.objects.filter(company_id=obj.id))
        elif rel_name == "job-applications":
            return "job-application", list(JobApplication.objects.filter(company_id=obj.id))
        return None, []


class CoverLetterSerializer(BaseSASerializer):
    type = "cover-letter"
    model = CoverLetter
    attributes = ["content", "created_at", "favorite", "status"]
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


class JobApplicationSerializer(BaseSASerializer):
    type = "job-application"
    model = JobApplication
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
        "questions": {"attr": "questions", "type": "question", "uselist": True},
        "application-statuses": {
            "attr": "application_statuses",
            "type": "job-application-status",
            "uselist": True,
        },
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
    attributes = ["content", "status"]
    relationships = {
        "user": {"attr": "user", "type": "user", "uselist": False},
        "job-post": {"attr": "job_post_id", "type": "job-post", "uselist": False},
    }
    relationship_fks = {
        "user": "user_id",
        "job-post": "job_post_id",
    }

    def to_resource(self, obj):
        d = {
            "type": self.type,
            "id": str(obj.id),
            "attributes": {"content": getattr(obj, "content", None)},
            "relationships": {},
        }
        if hasattr(obj, "user_id") and obj.user_id:
            d["relationships"]["user"] = {
                "data": {"type": "user", "id": str(obj.user_id)},
                "links": {
                    "self": f"{_resource_base_path(self.type)}/{obj.id}/relationships/user",
                    "related": f"{_resource_base_path('user')}/{obj.user_id}",
                },
            }
        if hasattr(obj, "job_post_id") and obj.job_post_id:
            d["relationships"]["job-post"] = {
                "data": {"type": "job-post", "id": str(obj.job_post_id)},
            }
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
                        d["attributes"]["active"] = bool(link.active)
        except Exception:
            pass
        return d

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


class DescriptionSerializer(BaseSASerializer):
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


class SkillSerializer(BaseSASerializer):
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


class JobApplicationStatusSerializer(BaseSASerializer):
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


class AnswerSerializer(BaseSASerializer):
    type = "answer"
    model = Answer
    attributes = ["content", "created_at", "favorite", "status"]
    relationships = {
        "question": {"attr": "question", "type": "question", "uselist": False},
    }
    relationship_fks = {"question": "question_id"}


class ApiKeySerializer(BaseSASerializer):
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


class QuestionSerializer(BaseSASerializer):
    type = "question"
    model = Question
    attributes = ["content", "created_at", "favorite"]
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
        return out


class ProjectSerializer(BaseSASerializer):
    type = "project"
    model = Project
    attributes = ["title", "description", "start_date", "end_date", "is_active", "created_at", "updated_at"]
    relationships = {
        "user": {"attr": "user", "type": "user", "uselist": False},
        "descriptions": {"attr": "descriptions", "type": "description", "uselist": True},
    }
    relationship_fks = {"user": "user_id"}


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
}
