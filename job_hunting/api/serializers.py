from datetime import date, datetime, timezone
from typing import Any, Dict, List

import dateparser
from django.contrib.auth import get_user_model
from django.db.models import Q
from rest_framework import serializers

from job_hunting.models import (
    Status, Skill, Description, Certification, Education, Summary,
    Company, ApiKey, Question, JobPost, JobPostDiscovery,
    Answer, JobApplication, CoverLetter, Experience, Resume, Score, Scrape,
    ExperienceDescription, ResumeSkill, ResumeSummary, JobApplicationStatus,
    Project, ResumeProject, ResumeExperience, ResumeEducation, ResumeCertification,
    AiUsage, Waitlist, Invitation, ScrapeProfile,
)
from job_hunting.models.job_post_dedupe import find_apply_url_matches


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
    # Legacy slim translation table. The wire `?slim=true` flag is
    # being retired in favor of JSON:API sparse-fieldsets
    # (`?fields[<type>]=...`). For the deprecation window, this list
    # is the equivalence map: when a request arrives with
    # `?slim=true`, `to_resource` emits exactly these attributes
    # (i.e. as if the client had asked
    # `?fields[<type>]=<slim_attributes joined>`). Subclasses still
    # override this list to declare their legacy slim shape.
    # Removal lands once every documented caller migrates; see the
    # follow-up todo on the parent for the cc-frontend half.
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

    def _requested_fieldset(self):
        """Return the explicit attribute list from `fields[<type>]`, or None
        when no sparse-fieldset filter applies. Honors JSON:API spec; only
        attributes already declared on the serializer are surfaced."""
        request = getattr(self, "request", None)
        if request is None:
            return None
        raw = request.query_params.get(f"fields[{self.type}]")
        if raw is None:
            return None
        requested = {s.strip() for s in str(raw).split(",") if s.strip()}
        declared = set(self.attributes)
        return [a for a in self.attributes if a in requested & declared]

    def _requested_includes(self) -> set:
        """Parse the request's `?include=` (and `?includes=`) into a set of
        relationship names matched against this serializer's relationships
        keys. Tolerant of dasherized/underscored variants. Returns the
        empty set when no include is requested."""
        request = getattr(self, "request", None)
        if request is None:
            return set()
        raw_parts = []
        for key in ("include", "includes"):
            val = request.query_params.get(key)
            if val:
                raw_parts.extend(s.strip() for s in str(val).split(",") if s.strip())
        if not raw_parts:
            return set()
        rel_keys = set(self.relationships.keys())
        out = set()
        for name in raw_parts:
            if name in rel_keys:
                out.add(name)
                continue
            dasher = name.replace("_", "-")
            if dasher in rel_keys:
                out.add(dasher)
        return out

    def _field_requested(self, name: str) -> bool:
        """For attributes built outside the declared `attributes` list (e.g.
        Resume.summary, which is computed in to_resource overrides rather
        than read off the model). Returns True when no fieldset filter is
        in effect; otherwise checks the raw requested set so callers can
        opt in to dynamic attributes by name."""
        request = getattr(self, "request", None)
        if request is None:
            return True
        raw = request.query_params.get(f"fields[{self.type}]")
        if raw is None:
            return True
        return name in {s.strip() for s in str(raw).split(",") if s.strip()}

    def to_resource(self, obj) -> Dict[str, Any]:
        # Legacy slim alias: `?slim=true` is equivalent to
        # `?fields[<type>]=<slim_attributes>` for the deprecation
        # window. Keep relationships/links emission consistent with the
        # JSON:API spec path by routing both through the same fieldset
        # filter; only the *source* of the requested attribute list
        # differs. Subclasses that consume `self.slim` for additional
        # side-effects (Resume's meta.counts, gated linked_relationships)
        # continue to do so explicitly.
        fieldset = self._requested_fieldset()
        if self.slim and fieldset is None:
            # Intersect with declared `attributes` so derived properties
            # named in slim_attributes but absent from `attributes` don't
            # crash getattr below. Existing behavior held: every entry in
            # ResumeSerializer.slim_attributes is already in attributes.
            declared = set(self.attributes)
            fieldset = [a for a in self.slim_attributes if a in declared]
        attrs_to_emit = self.attributes if fieldset is None else fieldset
        res = {
            "type": self.type,
            "id": str(obj.id),
            "attributes": {k: _to_primitive(getattr(obj, k)) for k in attrs_to_emit},
        }
        # JSON:API resource self link
        res["links"] = {"self": f"{_resource_base_path(self.type)}/{obj.id}"}
        if self.relationships:
            included_rels = self._requested_includes()
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
                    rel_payload: Dict[str, Any] = {"links": links}
                    # JSON:API spec: emitting `data` for a to-many asserts the
                    # complete linkage. Only emit when the client requested
                    # this relationship via `?include=` — i.e. when we are
                    # sideloading the items anyway. Without the linkage,
                    # Ember Data refetches via the related link even though
                    # the records are already in `included`. With it, the
                    # client resolves the sideload from one round trip.
                    if rel_name in included_rels:
                        try:
                            _, items = self.get_related(obj, rel_name)
                            rel_payload["data"] = [
                                {"type": rel_type, "id": str(item.id)} for item in items
                            ]
                        except Exception:
                            pass
                    rel_out[rel_name] = rel_payload
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
                # Pass the JSON:API id through verbatim (as a string) — do
                # NOT coerce to int. Relationship targets are mixed PK types
                # post CC-77: NanoID string PKs (company/job-post/scrape) vs
                # still-int PKs (user/status). Django coerces the string at
                # the model-field layer on create()/save() either way.
                out[fk_field] = str(rel["data"]["id"])
            elif rel and rel.get("data") is None:
                out[fk_field] = None
        return out


class DjangoUserSerializer:
    type = "user"
    model = get_user_model()

    # Declared relationships drive both the per-rel links block and the
    # `?include=` gate. Mirrors the (rel_name, rel_type, url_segment)
    # tuple used by the old eager-linkage loop, so get_related() and the
    # view's _build_included() keep working unchanged.
    _REL_DEFS = [
        ("resumes", "resume", "resumes"),
        ("scores", "score", "scores"),
        ("cover-letters", "cover-letter", "cover-letters"),
        ("job-applications", "job-application", "job-applications"),
        ("summaries", "summary", "summaries"),
    ]

    def accepted_types(self):
        return {self.type, _pluralize_type(self.type)}

    def _requested_includes(self) -> set:
        """Parse the request's ?include= (and ?includes=) into the set
        of relationship names this serializer should emit `data`
        linkage for. Tolerates dasherized/underscored variants. Returns
        the empty set when no include is requested OR when no request
        is attached — the latter is the read-time default that keeps
        /me/ JSON:API-compliant (links-only relationships).
        """
        request = getattr(self, "request", None)
        if request is None:
            return set()
        raw_parts = []
        for key in ("include", "includes"):
            val = request.query_params.get(key)
            if val:
                raw_parts.extend(
                    s.strip() for s in str(val).split(",") if s.strip()
                )
        if not raw_parts:
            return set()
        rel_keys = {name for name, _t, _u in self._REL_DEFS}
        out = set()
        for name in raw_parts:
            if name in rel_keys:
                out.add(name)
                continue
            dasher = name.replace("_", "-")
            if dasher in rel_keys:
                out.add(dasher)
        return out

    def _requested_fieldset(self, declared):
        """JSON:API `?fields[user]=...` parse. Returns a list of declared
        attribute names in the requested set, or None when no fieldset
        filter is in effect. Mirrors BaseSerializer._requested_fieldset
        but takes the declared set as an argument (the User serializer
        builds its attributes inline rather than from an `attributes`
        class var).
        """
        request = getattr(self, "request", None)
        if request is None:
            return None
        raw = request.query_params.get(f"fields[{self.type}]")
        if raw is None:
            return None
        requested = {s.strip() for s in str(raw).split(",") if s.strip()}
        return [a for a in declared if a in requested]

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
        federate_posts = False
        prof = None
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
                federate_posts = bool(getattr(prof, "federate_posts", False))
        except Exception:
            phone = ""
        if onboarding is None:
            from job_hunting.models import Profile
            onboarding = Profile.default_onboarding()
        # Derive `profile_basics` at read time so fresh signups aren't told
        # to fill in their name when they already did at signup. The other
        # derived flags wait for reconcile (read-time queries against
        # Resume/JobPost/Score would be too expensive on every user
        # serialize). When a Profile row exists we use the model helper so
        # the rule stays in sync with reconcile; otherwise we synthesize it.
        if prof is not None:
            onboarding["profile_basics"] = prof.profile_basics_complete()
        else:
            onboarding["profile_basics"] = bool(
                obj.first_name and obj.last_name and (obj.email or "")
            )

        all_attrs = {
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
            "federate_posts": federate_posts,
        }
        # JSON:API sparse-fieldsets: `?fields[user]=username,email`. Unknown
        # keys are silently dropped, matching the BaseSerializer behavior.
        fieldset = self._requested_fieldset(set(all_attrs.keys()))
        if fieldset is not None:
            all_attrs = {k: all_attrs[k] for k in fieldset}
        res = {
            "type": self.type,
            "id": str(obj.id),
            "attributes": all_attrs,
        }
        res["links"] = {"self": f"{_resource_base_path(self.type)}/{obj.id}"}

        # JSON:API: relationships objects hold `links` by default. The
        # `data` linkage array is only emitted when the client asked for
        # the relationship via `?include=`. Without this gate the /me/
        # response was hundreds of KB for power users (280+ scores,
        # 250+ job-applications, etc) and violated the spec.
        included_rels = self._requested_includes()
        relationships = {}
        for rel_name, rel_type, url_segment in self._REL_DEFS:
            rel_payload: Dict[str, Any] = {
                "links": {
                    "self": f"{_resource_base_path(self.type)}/{obj.id}/relationships/{rel_name}",
                    "related": f"{_resource_base_path(self.type)}/{obj.id}/{url_segment}",
                },
            }
            if rel_name in included_rels:
                try:
                    _, items = self.get_related(obj, rel_name)
                    rel_payload["data"] = [
                        {"type": rel_type, "id": str(item.id)} for item in items
                    ]
                except Exception:
                    rel_payload["data"] = []
            relationships[rel_name] = rel_payload
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
            "federate_posts",
        ]:
            if k in attrs_in:
                out[k] = attrs_in[k]
        # Accept hyphenated variants from JSON:API clients
        for hyphen, snake in [
            ("is-staff", "is_staff"),
            ("is-active", "is_active"),
            ("first-name", "first_name"),
            ("last-name", "last_name"),
            ("auto-score", "auto_score"),
            ("federate-posts", "federate_posts"),
        ]:
            if hyphen in attrs_in and snake not in out:
                out[snake] = attrs_in[hyphen]
        return out


class ResumeSerializer(BaseSerializer):
    type = "resume"
    model = Resume
    attributes = [
        "file_path", "title", "name", "notes", "user_id", "favorite", "status",
        "profession", "section_order", "effective_section_order",
    ]
    # effective_section_order is a computed @property on the model with no
    # setter — emitted for read convenience (so the frontend can avoid
    # recomputing the archetype-default fallback) but never round-tripped.
    # Without this, any Resume PATCH (including a favorite toggle) 500s
    # the moment Ember Data resends the field.
    read_only_attributes = ["effective_section_order"]
    slim_attributes = ["name", "title", "notes", "favorite", "profession"]
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
        # `meta.counts` is the historical slim-mode side-channel for
        # dropdown lists that need badge counts. Today it ships on
        # `?slim=true`. Forward path: `?meta=counts` is the explicit
        # opt-in that survives the slim retirement; the slim alias
        # implies it too during the deprecation window.
        if self.slim or self._meta_counts_requested():
            res["meta"] = self._build_counts(obj)
            if self.slim:
                # Existing slim contract: no `summary` convenience
                # attribute, no included sideloads — short-circuit.
                return res
        # Convenience attribute: active summary content. Respect
        # fields[resume] sparse-fieldsets — only emit when not filtered out.
        if self._field_requested("summary"):
            try:
                res.setdefault("attributes", {})["summary"] = obj.active_summary_content()
            except Exception:
                pass
        return res

    def _meta_counts_requested(self) -> bool:
        """True when the client opted into `?meta=counts` explicitly.
        Used as the forward-compat replacement for the slim-mode
        `meta.counts` side-channel."""
        request = getattr(self, "request", None)
        if request is None:
            return False
        raw = request.query_params.get("meta")
        if not raw:
            return False
        return "counts" in {s.strip() for s in str(raw).split(",") if s.strip()}

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


def compute_duplicate_candidates(post, request):
    """Return up to 10 likely-duplicate JobPost rows for `post`, decorated with
    `_match_signals` (set of signal strings) and `_confidence` (low/medium/high)
    so a downstream serializer can render them as job-post-duplicate-candidate
    resources.

    Single source of truth for both the standalone /duplicate-candidates/
    action and the `?include=duplicate-candidates` sideload path. Visibility
    mirrors JobPostViewSet.list — non-staff only see candidates they can
    otherwise reach via created_by / applications / scores / scrapes /
    discoveries. Excludes self and the duplicate_of chain in either direction.
    """
    user = getattr(request, "user", None) if request else None
    if user is not None and getattr(user, "is_staff", False):
        visible = JobPost.objects.all()
    else:
        uid = getattr(user, "id", None)
        if uid is None:
            return []
        visible = JobPost.objects.filter(
            Q(created_by_id=uid)
            | Q(applications__user_id=uid)
            | Q(scores__user_id=uid)
            | Q(scrapes__created_by_id=uid)
            | Q(discoveries__user_id=uid)
        ).distinct()

    excluded_ids = {post.id}
    if post.duplicate_of_id:
        excluded_ids.add(post.duplicate_of_id)
    excluded_ids.update(
        JobPost.objects.filter(duplicate_of_id=post.id).values_list("id", flat=True)
    )
    visible = visible.exclude(id__in=excluded_ids)

    candidates: Dict[int, Dict[str, Any]] = {}
    order = {"low": 0, "medium": 1, "high": 2}

    def _add(hit, signal, confidence):
        if hit.id in excluded_ids:
            return
        entry = candidates.setdefault(
            hit.id,
            {"post": hit, "signals": set(), "confidence": "low"},
        )
        entry["signals"].add(signal)
        if order[confidence] > order[entry["confidence"]]:
            entry["confidence"] = confidence

    if post.canonical_link:
        for hit in visible.filter(canonical_link=post.canonical_link):
            _add(hit, "canonical_link", "high")

    if post.content_fingerprint:
        for hit in visible.filter(content_fingerprint=post.content_fingerprint):
            _add(hit, "fingerprint", "high")

    # Phase B signal — slug-folded fingerprint catches punctuation-drift
    # twins ("Software Engineer - Product Security" U+002D hyphen vs
    # U+2013 en-dash) that the case+whitespace fold in ``fingerprint``
    # misses. Emitted as a separate signal code so the frontend can
    # surface BOTH reasons when both columns coincidentally agree
    # (the common case — _add stacks signals on the same candidate).
    #
    # Phase C upgrade — when the slug-fold matches but the candidate is
    # OLDER than ``settings.DEDUPE_REPOST_THRESHOLD_DAYS``, this is
    # almost certainly a repost (same role, new hiring cycle) rather
    # than the same active listing seen on a sibling channel. Emit the
    # ``"repost"`` reason code so the frontend duplicate-candidates
    # panel can route the user to the repost-link verb rather than the
    # collapse-duplicate verb. The threshold is configurable per
    # deployment via the env var of the same name; default 14 days.
    if post.normalized_fingerprint:
        from django.conf import settings as _dj_settings
        from datetime import timedelta
        from django.utils import timezone as _dj_tz
        threshold_days = getattr(
            _dj_settings, "DEDUPE_REPOST_THRESHOLD_DAYS", 14
        )
        repost_cutoff = _dj_tz.now() - timedelta(days=threshold_days)
        for hit in visible.filter(
            normalized_fingerprint=post.normalized_fingerprint
        ):
            hit_created = getattr(hit, "created_at", None)
            if hit_created is not None and hit_created < repost_cutoff:
                _add(hit, "repost", "high")
            else:
                _add(hit, "normalized_fingerprint", "high")

    # Cross-platform dedup via apply_url reciprocity. Shared primitive
    # with find_duplicate — see job_post_dedupe.find_apply_url_matches.
    for hit in find_apply_url_matches(post, base_qs=visible):
        _add(hit, "apply_hint", "high")

    # Referrer reciprocity stays inline (Scrape-side, not a destination).
    link_targets = [v for v in {post.link, post.canonical_link} if v]
    if link_targets:
        for hit in visible.filter(
            scrapes__referrer_url__in=link_targets
        ).distinct():
            _add(hit, "referrer_hint", "high")

    if post.company_id and post.title:
        t = post.title.strip()
        same_company = visible.filter(company_id=post.company_id).exclude(
            title__iexact=t
        )
        for hit in same_company[:200]:
            ht = (hit.title or "").strip()
            if not ht:
                continue
            a, b = t.lower(), ht.lower()
            if a.startswith(b) or b.startswith(a) or a.endswith(b) or b.endswith(a):
                _add(hit, "title_similarity", "medium")

    ordered = sorted(
        candidates.values(),
        key=lambda c: (
            -order[c["confidence"]],
            -(c["post"].created_at.timestamp() if c["post"].created_at else 0),
        ),
    )[:10]

    out = []
    for c in ordered:
        item = c["post"]
        item._match_signals = sorted(c["signals"])
        item._confidence = c["confidence"]
        out.append(item)
    return out


class JobPostDuplicateCandidateSerializer(BaseSerializer):
    """Virtual resource that renders a JobPost as a possible-duplicate row.

    Reads `_match_signals` and `_confidence` stashed on the obj by
    `compute_duplicate_candidates`. `model = JobPost` so the framework's
    `_build_included` can locate the underlying row when sideloading; the
    rendered shape is intentionally narrow (title / company_name / signals
    / confidence / frontend_url) and distinct from the full JobPost.
    """

    type = "job-post-duplicate-candidate"
    model = JobPost
    attributes: List[str] = []
    relationships: Dict[str, Dict[str, Any]] = {}

    def to_resource(self, obj) -> Dict[str, Any]:
        return {
            "type": self.type,
            "id": str(obj.id),
            "attributes": {
                "title": obj.title,
                "company_name": obj.company.name if obj.company_id else None,
                "match_signals": getattr(obj, "_match_signals", []),
                "confidence": getattr(obj, "_confidence", "low"),
                "frontend_url": f"/job-posts/{obj.id}",
            },
        }


class JobPostDiscoverySerializer(BaseSerializer):
    """Per-user provenance for a JobPost.

    JobPost is shared across users; the per-user "I know about this post"
    signal lives on JobPostDiscovery. Exposed as a hasMany on JobPost so
    the UI can render forwarder provenance (which catchall mailbox
    surfaced this listing) and so reports can audit ingest paths. The
    `requested_by` relationship is added by the Phase 2.5 staff-or-self
    RBAC ticket — staff API keys (cc_auto's) can attribute a discovery
    to a user other than the request's authenticated principal.
    """

    type = "job-post-discovery"
    model = JobPostDiscovery
    attributes = [
        "source",
        "forwarded_via_address",
        "created_at",
    ]
    user_fk = "user_id"
    relationships = {
        "job-post": {"attr": "job_post", "type": "job-post", "uselist": False},
        # Audit pointer: which user (the authenticated principal at write
        # time) drove this discovery. Equals `user` on every self-discover
        # path; differs only when a staff API key (cc_auto's) attributes
        # the row to another user via POST job-posts' `discover_for_user_id`.
        "requested-by": {"attr": "requested_by", "type": "user", "uselist": False},
    }
    relationship_fks = {
        "job-post": "job_post_id",
        "requested-by": "requested_by_id",
    }


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
        "canonical_link",
        "salary_min",
        "salary_max",
        "location",
        "remote",
        "top_score",
        "source",
        "apply_url",
        "apply_url_status",
        "apply_url_resolved_at",
        "posting_status",
        "complete",
        "duplicate_of_id",
        # Phase C dedupe redesign — repost relation. Distinct from
        # ``duplicate_of`` (collapse) — both rows stay queryable
        # independently. Frontend reads this on jp.edit/show to surface
        # "this role has been listed before"; writes happen via the
        # mark-duplicate-of verb with ``relation: "repost"``.
        "reposted_from_id",
        # ActivityPub-aligned per-post visibility (Phase 3.5 prep for
        # Phase 4 ActivityPub readiness). JSON list of AS2 audience URIs;
        # the frontend mirrors this via JobPost#isPublic for the Edit
        # form's Visibility selector and the show-page Private badge.
        "audience",
        # Phase 4 federation prep: which Career Caddy instance originated
        # this row. Read-only to clients — the API sets it from settings
        # on create, federation pull paths set it from the remote actor.
        "source_instance",
        # Phase 4 tombstone: timestamp at which the originating instance
        # broadcast an ActivityPub ``Delete`` for this row. NULL for
        # local-origin rows + any federated row whose origin hasn't
        # retracted it. Read-only — the inbound Delete handler is the
        # only write path. Surfaces to the frontend so the show / list
        # views can render the retraction banner without keeping a
        # second concept of "deleted" client-side.
        "source_deleted_at",
    ]
    read_only_attributes = ["source_instance", "source_deleted_at"]
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
        # Self-referential duplicate FK. The Ember frontend reads this on
        # jp.edit to show the current dup target (and lazy-fetch the row);
        # writes go through the mark-duplicate-of / unlink-duplicate /
        # promote-canonical verb endpoints, not via JSON:API PATCH.
        "duplicate-of": {
            "attr": "duplicate_of",
            "type": "job-post",
            "uselist": False,
        },
        # Phase C — self-FK to the original posting when ``relation:
        # "repost"`` was used on the mark-duplicate-of verb. Like
        # ``duplicate-of`` this is read-only on the JSON:API surface;
        # writes go through the verb endpoint, not PATCH.
        "reposted-from": {
            "attr": "reposted_from",
            "type": "job-post",
            "uselist": False,
        },
        # Virtual relationship: candidates are computed per-request, not a
        # Django reverse FK. attr name is intentionally non-existent — the
        # framework's getattr(obj, attr, None) returns None safely; the real
        # fetch happens in get_related() below. Kept out of linked_relationships
        # so the (expensive) candidate query only runs when the client passes
        # ?include=duplicate-candidates — see jp.show route.
        "duplicate-candidates": {
            "attr": "_duplicate_candidates_virtual",
            "type": "job-post-duplicate-candidate",
            "uselist": True,
        },
        # Per-user provenance hasMany. Records the catchall To-address
        # used for the Phase 2.5 email-forward source, and surfaces who
        # has signal on this shared post. Scoped to the requesting user
        # in `get_related` — discoveries are by definition per-user and
        # leaking other users' rows here would expose tenancy.
        "discoveries": {
            "attr": "discoveries",
            "type": "job-post-discovery",
            "uselist": True,
        },
    }
    relationship_fks = {"company": "company_id"}
    linked_relationships = [
        "scores", "questions", "summaries",
        "cover-letters", "job-applications",
    ]

    def get_related(self, obj, rel_name):
        request = getattr(self, "request", None)
        user_id = (
            getattr(getattr(request, "user", None), "id", None)
            if request else None
        )
        # Scores are per-user — only include the requesting caller's own scores
        # in the linkage. Without this filter the linked_relationships block
        # would embed IDs belonging to other users (including service-account
        # daemon scores), causing Ember Data to 404 on fetch and silently drop
        # them from the resolved hasMany.
        if rel_name == "scores":
            qs = obj.scores.filter(user_id=user_id) if user_id else obj.scores.none()
            return "score", list(qs)
        # `Summary.job_post_id` is a plain IntegerField (not a ForeignKey), so
        # there is no `obj.summaries` reverse accessor — query manually and
        # scope to the requesting user when we have one.
        if rel_name == "summaries":
            qs = Summary.objects.filter(job_post_id=obj.id)
            if user_id:
                qs = qs.filter(user_id=user_id)
            return "summary", list(qs)
        # Virtual relationship: candidates are synthesized from canonical_link
        # / fingerprint / title-suffix queries (no reverse FK), so we own the
        # fetch entirely. compute_duplicate_candidates stashes _match_signals
        # and _confidence on each row for JobPostDuplicateCandidateSerializer.
        if rel_name == "duplicate-candidates":
            return "job-post-duplicate-candidate", compute_duplicate_candidates(obj, request)
        # Discoveries are per-user — scope to the requesting caller. Staff
        # bypasses (they can see every signal) so an admin tooling view
        # can audit cross-user provenance; non-staff sees only their own
        # discoveries on this shared post.
        if rel_name == "discoveries":
            qs = obj.discoveries.all()
            if user_id and not getattr(getattr(request, "user", None), "is_staff", False):
                qs = qs.filter(user_id=user_id)
            return "job-post-discovery", list(qs)
        return super().get_related(obj, rel_name)

    def to_resource(self, obj):
        # Per-caller triage summary lives in JSON:API `meta`, not
        # `attributes`. JobPost is shared across users; the user's triage
        # status + reason + free-text note is NOT a property of the post,
        # it's a view-time derivation for the requesting user. Putting it
        # in `meta` keeps `attributes` honest about what's actually stored
        # on the JobPost row, and clients can't round-trip it back on a
        # PATCH as a resource field.
        #
        # Shape: meta.triage = { status, reason_code, note }
        # All three may be null when the caller has never triaged.
        res = super().to_resource(obj)
        triage = {
            "status": getattr(obj, "_active_application_status", None),
            "reason_code": getattr(obj, "_active_reason_code", None),
            "note": getattr(obj, "_active_reason_note", None),
        }
        res.setdefault("meta", {})["triage"] = triage

        # Privacy invariant: `top_score` (and the `top-score` relationship)
        # are PER-USER values on a shared JobPost row. The model property
        # has an unscoped fallback for non-request contexts (shell,
        # fixtures); the serializer must NEVER let that fallback surface
        # in an API response. The only way to emit a non-null top_score
        # is for the caller's view to attach `_top_score` filtered by
        # `request.user`. When `_top_score` is missing on the row we got
        # here, force-null both the attribute and the relationship —
        # this defends every sideload path (companies / job-applications
        # / score-include / etc.) the same way `_build_included` does
        # on the way in.
        #
        # Note: hasattr() is the right gate (not truthiness) because the
        # caller may have legitimately attached `None` (no Score row for
        # this user on this post) — that case must emit null, not fall
        # through to the unscoped `obj.scores.order_by("-score").first()`.
        if not hasattr(obj, "_top_score"):
            if "attributes" in res and "top_score" in res["attributes"]:
                res["attributes"]["top_score"] = None
            rels = res.setdefault("relationships", {})
            existing = rels.get("top-score") or {}
            existing_links = existing.get("links") or {
                "self": f"{_resource_base_path(self.type)}/{obj.id}/relationships/top-score",
            }
            rels["top-score"] = {"data": None, "links": existing_links}
        return res


class ScrapeSerializer(BaseSerializer):
    type = "scrape"
    model = Scrape
    attributes = [
        "url",
        "source_link",
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
        "apply_candidates",
        "skip_extract",
        "detected_posting_status",
        "detected_closed_evidence",
        # Phase A — Extension direct-POST plan. `source_mode` records HOW
        # the scrape was captured (browser tier vs extension content-
        # script); `captured_payload` carries the extension's pre-extracted
        # title/company/description shell when source_mode is
        # 'extension-direct'. See Scrape model docstrings for shape.
        "source_mode",
        "captured_payload",
        # Attended-scrape routing. Writable snake_case boolean (model
        # default False). When True, only an attended runner can claim
        # the resulting hold scrape via POST /scrapes/claim-next/. cc_auto
        # sends `attended: true` to route a scrape to a human-driven
        # headed browser; omitted/False keeps the default runner queue.
        "attended",
        # Operator-facing diagnostic populated by the failed-status
        # write sites (placeholder rejection, parse_scrape exception,
        # CompletenessReviewer rejection, sweep). Read-only on the
        # wire so client-side echoes can't poison it; mutation only
        # happens through the documented write paths in lib/scraper.py
        # and lib/parsers/job_post_extractor.py.
        "failure_reason",
    ]
    # latest_status_note is a derived @property on Scrape with no setter —
    # output it but reject it on PATCH so frontend round-trips don't 500
    # with "property has no setter". failure_reason has a real column
    # but is operator-diagnostic state — clients must never overwrite
    # it via PATCH.
    read_only_attributes = ["latest_status_note", "failure_reason"]
    relationships = {
        "job-post": {"attr": "job_post", "type": "job-post", "uselist": False},
        "company": {"attr": "company", "type": "company", "uselist": False},
        "scrape-statuses": {"attr": "scrape_statuses", "type": "scrape-status", "uselist": True},
    }
    relationship_fks = {"job-post": "job_post_id", "company": "company_id"}

    # Phase A — required fields on a `source_mode='extension-direct'`
    # capture payload. "Trust presence, iterate" is Doug's v1 rule (plan
    # /Plans/PLAN Extension direct-POST when capture is complete): the
    # gate is non-empty title + company + description. No confidence
    # threshold, no LLM-side validator — we trust the user-rendered DOM
    # and surface false-positives only if they show up in the wild.
    _EXTENSION_DIRECT_REQUIRED_FIELDS = ("title", "company", "description")

    def parse_payload(self, payload):
        """Validate the source_mode / captured_payload pair before persistence.

        Wraps BaseSerializer.parse_payload so PATCH round-trips through the
        same gate POST does. Three rules — message shapes mirror the
        EmailForwardSourceTests pattern (single-line ``detail`` naming the
        offending field) so the frontend / extension can branch on the
        field token without parsing prose:

        - ``source_mode='extension-direct'`` REQUIRES ``captured_payload``
          to be a dict with non-empty string values for title, company,
          description.
        - ``source_mode='browser'`` (the default) MUST NOT carry a
          ``captured_payload`` — surfaces an authorial confusion where a
          paste path leaks a stale field (same defensive shape the
          email-forward path uses for ``forwarded_via_address``).
        - Any other ``source_mode`` value is rejected — Scrape model
          choices currently only allow the two above.
        """
        attrs = super().parse_payload(payload)
        validate_scrape_source_mode_payload(attrs)
        return attrs


def validate_scrape_source_mode_payload(attrs):
    """Enforce the Phase A source_mode / captured_payload contract.

    Mutates nothing; raises ``ValueError`` with a single-line message
    naming the offending field. Both ScrapeSerializer.parse_payload (the
    PATCH path through BaseViewSet._upsert) and
    ScrapeViewSet.create (the custom POST that bypasses serializer-based
    create) call this so the rules hold on every write path.

    Skips validation entirely when neither ``source_mode`` nor
    ``captured_payload`` appears in ``attrs`` — the JobPost-extractor /
    apply-resolver / claim-next PATCHes don't touch these fields and
    must not be forced to echo the model default back.
    """
    has_source_mode = "source_mode" in attrs
    has_payload = "captured_payload" in attrs
    if not (has_source_mode or has_payload):
        return

    # When only payload arrives without an explicit source_mode, treat
    # the absence as "the row already has a source_mode and we're only
    # touching the payload". The PATCH path through _upsert pre-fills
    # nothing about existing fields, so a partial update that sets the
    # payload alone is a legitimate Phase B flow (re-run capture against
    # an already-extension-direct scrape). Skip the cross-field check.
    if has_payload and not has_source_mode:
        return

    source_mode = attrs.get("source_mode")
    payload = attrs.get("captured_payload")

    valid_modes = {choice for choice, _ in Scrape.SOURCE_MODE_CHOICES}
    if source_mode not in valid_modes:
        # Reject unknown values up front. Mirrors the choices the model
        # would reject on .save() anyway — surfacing it here turns a
        # 500 (IntegrityError from the DB constraint Django emits for
        # CharField choices is actually a noop, but a future db-level
        # CHECK would crash) into a clean 400.
        raise ValueError(
            f"source_mode must be one of {sorted(valid_modes)}"
        )

    if source_mode == "extension-direct":
        if payload is None:
            raise ValueError(
                "captured_payload is required when "
                "source_mode='extension-direct'"
            )
        if not isinstance(payload, dict):
            raise ValueError(
                "captured_payload must be an object when "
                "source_mode='extension-direct'"
            )
        for field in ScrapeSerializer._EXTENSION_DIRECT_REQUIRED_FIELDS:
            value = payload.get(field)
            if not isinstance(value, str) or not value.strip():
                # Single-line detail naming the field — mirrors
                # EmailForwardSourceTests rejection shape so the
                # extension can branch on the field token, not prose.
                raise ValueError(
                    f"captured_payload.{field} is required when "
                    "source_mode='extension-direct'"
                )
        return

    # source_mode == 'browser' — payload must be absent or NULL. A
    # browser-mode write that carries a payload is almost certainly a
    # client bug echoing a stale field; refuse it loudly so the bug
    # surfaces instead of writing a half-fast-path row.
    if payload is not None:
        raise ValueError(
            "captured_payload is only valid when "
            "source_mode='extension-direct'"
        )


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
    attributes = [
        "name",
        "display_name",
        "notes",
        "source",
        "name_slug",
        # Phase 6a — federation handle + opt-in toggle. Both safe to
        # surface on the read path (slug is the public WebFinger
        # handle; federation_enabled is operator-visible state).
        # PATCH on these flows through ``CompanyViewSet.update``,
        # which only allows the legacy field set today — the frontend
        # dispatch follows separately.
        "slug",
        "federation_enabled",
    ]
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
        # Phase A self-FK alias relationships.
        # `canonical` (to-one) is the forward FK — NULL when this
        # row IS canonical. `aliases` (to-many) is the reverse —
        # every Company whose canonical_id == self.id.
        # Both follow the spec's links-only-by-default rule. The
        # `data` linkage emits only when the client passes
        # `?include=canonical` / `?include=aliases`.
        "canonical": {"attr": "canonical", "type": "company", "uselist": False},
        "aliases": {"attr": "aliases", "type": "company", "uselist": True},
    }
    relationship_fks = {"canonical": "canonical_id"}

    def get_related(self, obj, rel_name):
        request = getattr(self, "request", None)
        user_id = getattr(getattr(request, "user", None), "id", None) if request else None
        if rel_name == "job-posts":
            qs = JobPost.objects.filter(company_id=obj.id)
            # Visibility filter mirrors JobPostViewSet.list — without
            # `scrapes` and `discoveries` here, sideloaded company.job-posts
            # disagrees with what /companies/<id>/job-posts/ returns.
            if user_id:
                is_staff = bool(getattr(getattr(request, "user", None), "is_staff", False))
                if not is_staff:
                    qs = qs.filter(
                        Q(created_by_id=user_id) |
                        Q(applications__user_id=user_id) |
                        Q(scores__user_id=user_id) |
                        Q(scrapes__created_by_id=user_id) |
                        Q(discoveries__user_id=user_id)
                    ).distinct()
            return "job-post", list(qs)
        elif rel_name == "job-applications":
            qs = JobApplication.objects.filter(company_id=obj.id)
            if user_id:
                qs = qs.filter(user_id=user_id)
            return "job-application", list(qs)
        elif rel_name == "aliases":
            return "company", list(Company.objects.filter(canonical_id=obj.id))
        elif rel_name == "canonical":
            if obj.canonical_id is None:
                return "company", []
            target = Company.objects.filter(pk=obj.canonical_id).first()
            return "company", [target] if target else []
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
                        resume_id=resume_id, summary_id=obj.id
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
                        resume_id=resume_id, skill_id=obj.id
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
    attributes = ["created_at", "logged_at", "note", "reason_code"]
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
        "extension_selectors",
        "extraction_hints", "page_structure",
        "last_success_at", "scrape_count", "failure_count", "tier0_miss_count",
        "preferred_tier", "enabled", "is_known_good", "created_at", "updated_at",
    ]
    # `is_known_good` is a computed @property with no setter; flag it read-only
    # so any inbound payload carrying it is dropped instead of crashing setattr.
    read_only_attributes = ["is_known_good"]
    relationships = {}
    relationship_fks = {}

    def to_resource(self, obj):
        res = super().to_resource(obj)
        # `readiness()` is a method (returns a debug struct), so it can't ride
        # the getattr-based attributes loop. Inject it here for the
        # /admin/scrape-profiles debug panel, honoring sparse-fieldset opt-out
        # (?fields[scrape-profile]=...). Cheap: pure in-memory field reads.
        if self._field_requested("readiness"):
            res.setdefault("attributes", {})["readiness"] = obj.readiness()
        return res


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
    "job-post-duplicate-candidate": JobPostDuplicateCandidateSerializer,
    "job-post-discovery": JobPostDiscoverySerializer,
}
