import dateparser
import logging

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from drf_spectacular.utils import (
    extend_schema,
    extend_schema_view,
    OpenApiResponse,
    inline_serializer,
    OpenApiParameter,
)
from drf_spectacular.types import OpenApiTypes
from rest_framework import serializers as drf_serializers

from .base import BaseViewSet
from ._schema import (
    _INCLUDE_PARAM,
    _PAGE_PARAMS,
    _JSONAPI_LIST,
    _JSONAPI_ITEM,
    _JSONAPI_WRITE,
)
from ..serializers import (
    ResumeSerializer,
    ScoreSerializer,
    CoverLetterSerializer,
    JobApplicationSerializer,
    SummarySerializer,
    ExperienceSerializer,
    EducationSerializer,
    SkillSerializer,
    _parse_date,
)
from job_hunting.lib.ai_client import get_client
from job_hunting.lib.services.summary_service import SummaryService
from job_hunting.lib.services.ingest_resume import IngestResume
from job_hunting.models import (
    Summary,
    Description,
    Certification,
    Education,
    Skill,
    JobPost,
    CoverLetter,
    Experience,
    Resume,
    ExperienceDescription,
    ResumeEducation,
    ResumeCertification,
    ResumeSummary,
    ResumeExperience,
    ResumeProject,
    ResumeSkill,
)

logger = logging.getLogger(__name__)


@extend_schema_view(
    create=extend_schema(tags=["Resumes"], summary="Create a resume"),
    update=extend_schema(
        tags=["Resumes"],
        summary="Update a resume (supports nested experiences/educations/skills reconciliation)",
    ),
    partial_update=extend_schema(
        tags=["Resumes"],
        summary="Partially update a resume (supports nested experiences/educations/skills reconciliation)",
    ),
    destroy=extend_schema(tags=["Resumes"], summary="Delete a resume"),
)
class ResumeViewSet(BaseViewSet):
    model = Resume
    serializer_class = ResumeSerializer
    _default_includes = ["summaries", "certifications", "educations", "experiences", "skills", "projects"]

    @extend_schema(
        tags=["Resumes"],
        summary="List resumes (auto-includes all relationships)",
        parameters=_PAGE_PARAMS,
        responses={200: _JSONAPI_LIST},
    )
    def list(self, request):
        items = list(self.model.objects.filter(user_id=request.user.id))
        items = self.paginate(items)
        slim = self._is_slim_request(request)
        ser = self.get_serializer(slim=slim)
        data = [ser.to_resource(o) for o in items]
        payload = {"data": data}
        if not slim:
            include_rels = self._parse_include(request) or self._default_includes
            payload["included"] = self._build_included(items, include_rels, request)
        return Response(payload)

    @extend_schema(
        tags=["Resumes"],
        summary="Retrieve a resume (auto-includes all relationships)",
        parameters=[_INCLUDE_PARAM],
        responses={200: _JSONAPI_ITEM, 404: OpenApiResponse(description="Not found")},
    )
    def retrieve(self, request, pk=None):
        obj = self.model.objects.filter(pk=int(pk)).first()
        if not obj or obj.user_id != request.user.id:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer()
        payload = {"data": ser.to_resource(obj)}
        include_rels = self._parse_include(request) or self._default_includes
        payload["included"] = self._build_included([obj], include_rels, request)
        return Response(payload)

    def _upsert(self, request, pk, partial=False):
        obj = Resume.objects.filter(pk=int(pk)).first()
        if not obj or obj.user_id != request.user.id:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response({"errors": [{"detail": str(e)}]}, status=400)

        # Update scalar attributes
        for k, v in attrs.items():
            setattr(obj, k, v)
        obj.save()

        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") or {}
        attrs_node = node.get("attributes") or {}
        # Merge relationships from both the standard location and the nested "data" block
        # (some clients send a nested data.data.relationships for sub-relationships)
        nested_rels = (node.get("data") or {}).get("relationships") or {}
        rels_node = {**(node.get("relationships") or {}), **nested_rels}

        # Optional: update active summary content if attributes.summary is provided
        incoming_summary = attrs_node.get("summary")
        if isinstance(incoming_summary, str):
            new_content = incoming_summary.strip()
            # Find active link
            active_link = ResumeSummary.objects.filter(
                resume_id=obj.id, active=True
            ).first()
            if active_link:
                sm = Summary.objects.filter(pk=active_link.summary_id).first()
                if sm and (sm.content or "") != new_content:
                    sm.content = new_content
                    sm.save()
                # Ensure only this one is active
                ResumeSummary.objects.filter(resume_id=obj.id).exclude(
                    pk=active_link.id
                ).update(active=False)
                active_link.active = True
                active_link.save()
            else:
                # No active summary; create one and activate it
                sm = Summary.objects.create(
                    job_post_id=None,
                    user_id=getattr(obj, "user_id", None),
                    content=new_content,
                )
                ResumeSummary.objects.filter(resume_id=obj.id).update(active=False)
                ResumeSummary.objects.create(
                    resume_id=obj.id, summary_id=sm.id, active=True
                )

        # Helpers for relationship reconciliation
        def _int_or_none(v):
            try:
                return int(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        def _dp(val):
            if not val:
                return None
            try:
                dt = dateparser.parse(str(val))
                return dt.date() if dt else None
            except Exception:
                return None

        # Experiences: if present, PATCH to match provided set and update Experience attributes/links
        experiences_in = node.get("experiences") or data.get("experiences")
        if experiences_in is not None:
            desired_exp_ids = []
            for wrapper in experiences_in or []:
                exp_node = (wrapper or {}).get("data") or wrapper or {}
                exp_id = _int_or_none(exp_node.get("id"))
                if not exp_id:
                    return Response(
                        {"errors": [{"detail": "Experience id is required in PATCH"}]},
                        status=400,
                    )
                exp = Experience.objects.filter(pk=exp_id).first()
                if not exp:
                    return Response(
                        {"errors": [{"detail": f"Invalid experience id: {exp_id}"}]},
                        status=400,
                    )

                # Update experience attributes
                exp_attrs = exp_node.get("attributes") or {}
                if "title" in exp_attrs:
                    exp.title = exp_attrs.get("title")
                if "location" in exp_attrs:
                    exp.location = exp_attrs.get("location")
                if "summary" in exp_attrs:
                    exp.summary = exp_attrs.get("summary")
                if "start_date" in exp_attrs:
                    exp.start_date = _dp(exp_attrs.get("start_date"))
                if "end_date" in exp_attrs:
                    exp.end_date = _dp(exp_attrs.get("end_date"))

                # Update company relation (optional)
                exp_rels = exp_node.get("relationships") or {}
                comp_rel = (exp_rels.get("company") or {}).get("data") or {}
                comp_id = _int_or_none(comp_rel.get("id"))
                if comp_rel:
                    exp.company_id = comp_id

                exp.save()

                # Ensure resume <-> experience link exists
                ResumeExperience.objects.get_or_create(
                    resume_id=obj.id, experience_id=exp.id
                )

                # Reconcile descriptions for this experience (keep order as provided)
                desc_nodes = (exp_rels.get("descriptions") or {}).get("data") or []
                desired_desc_ids_ordered = []
                invalid_desc_ids = []
                for d in desc_nodes:
                    did = _int_or_none((d or {}).get("id"))
                    if did is None:
                        continue
                    if not Description.objects.filter(pk=did).exists():
                        invalid_desc_ids.append(did)
                        continue
                    desired_desc_ids_ordered.append(did)
                if invalid_desc_ids:
                    return Response(
                        {
                            "errors": [
                                {
                                    "detail": f"Invalid description ID(s): {', '.join(map(str, invalid_desc_ids))}"
                                }
                            ]
                        },
                        status=400,
                    )

                if desc_nodes is not None:
                    existing_links = list(
                        ExperienceDescription.objects.filter(experience_id=exp.id)
                    )
                    existing_by_desc = {
                        lnk.description_id: lnk for lnk in existing_links
                    }
                    desired_set = set(desired_desc_ids_ordered)
                    existing_set = set(existing_by_desc.keys())

                    # Remove links not desired
                    to_remove = existing_set - desired_set
                    if to_remove:
                        ExperienceDescription.objects.filter(
                            experience_id=exp.id,
                            description_id__in=list(to_remove),
                        ).delete()

                    # Add/update desired links with order
                    for order_idx, did in enumerate(desired_desc_ids_ordered):
                        link = existing_by_desc.get(did)
                        if not link:
                            ExperienceDescription.objects.create(
                                experience_id=exp.id,
                                description_id=did,
                                order=order_idx,
                            )
                        else:
                            link.order = order_idx
                            link.save()

                desired_exp_ids.append(exp.id)

            # Reconcile resume_experience set to match provided experiences
            existing_links = list(ResumeExperience.objects.filter(resume_id=obj.id))
            existing_ids = {lnk.experience_id for lnk in existing_links}
            desired_ids = set(desired_exp_ids)
            to_add = desired_ids - existing_ids
            to_remove = existing_ids - desired_ids

            for eid in to_add:
                ResumeExperience.objects.get_or_create(
                    resume_id=obj.id, experience_id=eid
                )
            if to_remove:
                ResumeExperience.objects.filter(
                    resume_id=obj.id,
                    experience_id__in=list(to_remove),
                ).delete()

        # Educations: reconcile join set if provided
        educations_in = node.get("educations") or data.get("educations")
        if educations_in is not None:
            desired_ids = set()
            invalid = []
            for item in educations_in or []:
                ed_node = (item or {}).get("data") or item or {}
                eid = _int_or_none(ed_node.get("id"))
                if eid is None:
                    continue
                if not Education.objects.filter(pk=eid).exists():
                    invalid.append(eid)
                else:
                    desired_ids.add(eid)
            if invalid:
                return Response(
                    {
                        "errors": [
                            {
                                "detail": f"Invalid education ID(s): {', '.join(map(str, invalid))}"
                            }
                        ]
                    },
                    status=400,
                )
            existing_ids = set(
                ResumeEducation.objects.filter(resume_id=obj.id).values_list(
                    "education_id", flat=True
                )
            )
            to_add = desired_ids - existing_ids
            to_remove = existing_ids - desired_ids
            for eid in to_add:
                ResumeEducation.objects.create(resume_id=obj.id, education_id=eid)
            if to_remove:
                ResumeEducation.objects.filter(
                    resume_id=obj.id,
                    education_id__in=list(to_remove),
                ).delete()

        # Certifications: reconcile join set if provided
        certifications_in = node.get("certifications") or data.get("certifications")
        if certifications_in is not None:
            desired_ids = set()
            invalid = []
            for item in certifications_in or []:
                c_node = (item or {}).get("data") or item or {}
                cid = _int_or_none(c_node.get("id"))
                if cid is None:
                    continue
                if not Certification.objects.filter(pk=cid).exists():
                    invalid.append(cid)
                else:
                    desired_ids.add(cid)
            if invalid:
                return Response(
                    {
                        "errors": [
                            {
                                "detail": f"Invalid certification ID(s): {', '.join(map(str, invalid))}"
                            }
                        ]
                    },
                    status=400,
                )
            existing_ids = set(
                ResumeCertification.objects.filter(resume_id=obj.id).values_list(
                    "certification_id", flat=True
                )
            )
            to_add = desired_ids - existing_ids
            to_remove = existing_ids - desired_ids
            for cid in to_add:
                ResumeCertification.objects.create(
                    resume_id=obj.id, certification_id=cid
                )
            if to_remove:
                ResumeCertification.objects.filter(
                    resume_id=obj.id,
                    certification_id__in=list(to_remove),
                ).delete()

        # Skills: reconcile join set if provided
        skills_in = node.get("skills") or data.get("skills")
        if skills_in is not None:
            desired_active_by_id = {}
            invalid = []
            for item in skills_in or []:
                if not isinstance(item, dict):
                    continue
                s_node = (item.get("data") or item) or {}
                sid = _int_or_none(s_node.get("id"))
                skill = None
                if sid is not None:
                    skill = Skill.objects.filter(pk=sid).first()
                    if not skill:
                        invalid.append(sid)
                        continue
                else:
                    s_attrs = s_node.get("attributes") or {}
                    text = s_attrs.get("text") or s_node.get("text")
                    if not text:
                        continue
                    # Create or find by text
                    skill, _ = Skill.objects.get_or_create(text=str(text).strip())

                # Determine desired active flag (default True)
                active_val = (s_node.get("attributes") or {}).get("active")
                active_val = bool(active_val) if active_val is not None else True
                desired_active_by_id[skill.id] = active_val

            if invalid:
                return Response(
                    {
                        "errors": [
                            {
                                "detail": f"Invalid skill ID(s): {', '.join(map(str, invalid))}"
                            }
                        ]
                    },
                    status=400,
                )

            existing_links = list(ResumeSkill.objects.filter(resume_id=obj.id))
            existing_ids = {lnk.skill_id for lnk in existing_links}
            desired_ids = set(desired_active_by_id.keys())

            to_add = desired_ids - existing_ids
            to_remove = existing_ids - desired_ids
            to_update = desired_ids & existing_ids

            # Remove undesired links
            if to_remove:
                ResumeSkill.objects.filter(
                    resume_id=obj.id,
                    skill_id__in=list(to_remove),
                ).delete()

            # Add missing links
            for sid in to_add:
                ResumeSkill.objects.get_or_create(
                    resume_id=obj.id,
                    skill_id=sid,
                    defaults={"active": desired_active_by_id[sid]},
                )

            # Update 'active' where needed
            for link in existing_links:
                if link.skill_id in to_update:
                    desired_active = desired_active_by_id[link.skill_id]
                    if bool(link.active) != bool(desired_active):
                        link.active = bool(desired_active)
                        link.save()

        # Summaries: reconcile join set if provided
        # Accept from node["summaries"], data["summaries"], or relationships.summaries.data
        _rel_summaries = (rels_node.get("summaries") or {}).get("data")
        summaries_in = node.get("summaries") or data.get("summaries") or _rel_summaries
        if summaries_in is not None:
            desired_ids_ordered = []  # preserve order — last item becomes active
            invalid = []
            desired_active_sid = None  # explicit active=true flag wins
            for item in summaries_in or []:
                s_node = (item or {}).get("data") or item or {}
                sid = _int_or_none(s_node.get("id"))
                if sid is None:
                    continue
                if not Summary.objects.filter(pk=sid).exists():
                    invalid.append(sid)
                else:
                    if sid not in desired_ids_ordered:
                        desired_ids_ordered.append(sid)
                    active_flag = (s_node.get("attributes") or {}).get("active")
                    if active_flag:
                        desired_active_sid = sid
            if invalid:
                return Response(
                    {"errors": [{"detail": f"Invalid summary ID(s): {', '.join(map(str, invalid))}"}]},
                    status=400,
                )

            desired_ids = set(desired_ids_ordered)
            existing_links = list(ResumeSummary.objects.filter(resume_id=obj.id))
            existing_ids = {lnk.summary_id for lnk in existing_links}

            to_add = desired_ids - existing_ids
            to_remove = existing_ids - desired_ids

            for sid in to_add:
                ResumeSummary.objects.create(resume_id=obj.id, summary_id=sid, active=False)
            if to_remove:
                ResumeSummary.objects.filter(
                    resume_id=obj.id, summary_id__in=list(to_remove)
                ).delete()

            # Determine which summary should be active:
            # 1. explicit active=true flag on an item, else
            # 2. last item in the provided list
            active_sid = desired_active_sid or (desired_ids_ordered[-1] if desired_ids_ordered else None)
            if active_sid:
                ResumeSummary.objects.filter(resume_id=obj.id).update(active=False)
                ResumeSummary.objects.filter(
                    resume_id=obj.id, summary_id=active_sid
                ).update(active=True)

        # Always enforce exactly one active summary
        ResumeSummary.ensure_single_active_for_resume(obj.id)

        payload = {"data": ser.to_resource(obj)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([obj], include_rels, request)
        return Response(payload)

    def create(self, request):
        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response(
                {"errors": [{"detail": str(e)}]}, status=status.HTTP_400_BAD_REQUEST
            )

        # Inject user_id from relationships ("user" or "users") into attrs for creation
        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") or {}
        relationships = node.get("relationships") or {}

        # Extract user_id from multiple possible shapes:
        # - data.relationships.user(.data.id)
        # - data.user / data.users (object or RI)
        # - data.attributes.user_id | user-id | userId
        # - top-level user_id | user-id | userId
        def _first_id(node_val):
            if isinstance(node_val, dict):
                d = node_val.get("data", node_val)
                if isinstance(d, dict) and "id" in d:
                    return d.get("id")
                if isinstance(d, list) and d:
                    first = d[0]
                    if isinstance(first, dict) and "id" in first:
                        return first["id"]
            elif node_val is not None:
                return node_val
            return None

        user_id_val = None

        # relationships.user|users
        rel_user = relationships.get("user") or relationships.get("users")
        if isinstance(rel_user, dict):
            user_id_val = _first_id(rel_user)

        # node-level user|users
        if user_id_val is None:
            user_id_val = _first_id(node.get("user") or node.get("users"))

        # attributes user_id variants
        if user_id_val is None:
            attrs_node = node.get("attributes") or {}
            user_id_val = (
                attrs_node.get("user_id")
                or attrs_node.get("user-id")
                or attrs_node.get("userId")
            )

        # top-level convenience keys
        if user_id_val is None:
            user_id_val = (
                data.get("user_id") or data.get("user-id") or data.get("userId")
            )

        # Default to authenticated user if no user_id provided
        if user_id_val is None and request.user.is_authenticated:
            user_id_val = request.user.id

        if user_id_val is not None:
            try:
                user_id_int = int(user_id_val)
            except (TypeError, ValueError):
                return Response(
                    {"errors": [{"detail": "Invalid user ID"}]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            User = get_user_model()
            if not User.objects.filter(id=user_id_int).exists():
                return Response(
                    {"errors": [{"detail": "Invalid user ID"}]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            attrs["user_id"] = user_id_int

        # Create the resume
        resume = Resume.objects.create(**attrs)

        # Extract potential child arrays from JSON:API attributes or top-level convenience keys
        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") or {}
        attrs_node = node.get("attributes") or {}
        experiences_in = node.get("experiences") or data.get("experiences") or []
        educations_in = node.get("educations") or data.get("educations") or []
        certifications_in = (
            node.get("certifications") or data.get("certifications") or []
        )
        summaries_in = node.get("summaries") or data.get("summaries") or []

        # Helpers
        def _int_or_none(v):
            try:
                return int(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        def _dp(val):
            if not val:
                return None
            try:
                dt = dateparser.parse(str(val))
                return dt.date() if dt else None
            except Exception:
                return None

        # Upsert Experiences and link to resume; also upsert/link nested descriptions
        for item in experiences_in or []:
            if not isinstance(item, dict):
                continue
            exp = None
            # Support explicit id if provided
            eid = (item.get("data") or {}).get("id") or item.get("id")
            if eid:
                exp = Experience.objects.filter(pk=eid).first()

            # Helper to normalize company_id
            rels = item.get("relationships") or {}
            company_rel = rels.get("company") or {}
            company_id = item.get("company_id") or (company_rel.get("data") or {}).get(
                "id"
            )
            company_id = int(company_id) if company_id is not None else None

            # Helper to collect incoming description lines in order
            incoming_lines = []
            if isinstance(item.get("description_lines"), list):
                incoming_lines = [
                    str(s).strip() for s in item["description_lines"] if str(s).strip()
                ]
            elif isinstance(item.get("descriptions"), list):
                # Accept either content or reference by id
                for d in item["descriptions"]:
                    if not isinstance(d, dict):
                        continue
                    if "content" in d and d["content"]:
                        incoming_lines.append(str(d["content"]).strip())
                    elif "id" in d and d["id"]:
                        dd = Description.objects.filter(pk=int(d["id"])).first()
                        if dd and getattr(dd, "content", None):
                            incoming_lines.append(dd.content.strip())

            # Parse dates via dateparser
            s_date = _dp(item.get("start_date"))
            e_date = _dp(item.get("end_date"))

            if exp is None:
                # Try to find an existing experience with matching scalars and identical description list
                candidates = list(
                    Experience.objects.filter(
                        company_id=company_id,
                        title=item.get("title"),
                        location=item.get("location"),
                        summary=(item.get("summary") or ""),
                        start_date=s_date,
                        end_date=e_date,
                    )
                )
                match = None
                for cand in candidates:
                    try:
                        existing_lines = [
                            d.content.strip()
                            for d in (cand.descriptions or [])
                            if getattr(d, "content", None)
                        ]
                    except Exception:
                        existing_lines = []
                    if existing_lines == incoming_lines:
                        match = cand
                        break

                if match:
                    exp = match
                else:
                    # Create new experience
                    exp = Experience.objects.create(
                        company_id=company_id,
                        title=item.get("title"),
                        location=item.get("location"),
                        summary=(item.get("summary") or ""),
                        start_date=s_date,
                        end_date=e_date,
                        content=item.get("content"),
                    )
                    # Link descriptions in order, creating Description rows as needed
                    ExperienceDescription.objects.filter(experience_id=exp.id).delete()
                    for idx, line in enumerate(incoming_lines or []):
                        if not line:
                            continue
                        desc, _ = Description.objects.get_or_create(content=line)
                        ExperienceDescription.objects.create(
                            experience_id=exp.id,
                            description_id=desc.id,
                            order=idx,
                        )

            # Join resume_experience (avoid duplicates)
            ResumeExperience.objects.get_or_create(
                resume_id=resume.id,
                experience_id=exp.id,
            )

            # Nested descriptions for this experience
            for d in item.get("descriptions") or []:
                if not isinstance(d, dict):
                    continue
                desc = None
                did = _int_or_none(d.get("id"))
                if did:
                    desc = Description.objects.filter(pk=did).first()
                if desc is None:
                    content = d.get("content")
                    if not content:
                        continue
                    desc, _ = Description.objects.get_or_create(content=content)
                # Link with optional order
                order = d.get("order")
                if order is None and isinstance(d.get("meta"), dict):
                    order = d["meta"].get("order")
                try:
                    order = int(order) if order is not None else 0
                except (TypeError, ValueError):
                    order = 0
                ExperienceDescription.objects.get_or_create(
                    experience_id=exp.id,
                    description_id=desc.id,
                    defaults={"order": order},
                )

        # Upsert Educations and link
        for item in educations_in or []:
            if not isinstance(item, dict):
                continue
            edu = None
            eid = _int_or_none(item.get("id"))
            if eid:
                edu = Education.objects.filter(pk=eid).first()
            if edu is None:
                lookup = {
                    "institution": item.get("institution"),
                    "degree": item.get("degree"),
                    "major": item.get("major"),
                    "minor": item.get("minor"),
                    "issue_date": _parse_date(item.get("issue_date")),
                }
                lookup = {k: v for k, v in lookup.items() if v is not None}
                edu = Education.objects.filter(**lookup).first()
                if not edu:
                    create_attrs = {
                        "institution": item.get("institution"),
                        "degree": item.get("degree"),
                        "major": item.get("major"),
                        "minor": item.get("minor"),
                        "issue_date": _parse_date(item.get("issue_date")),
                    }
                    create_attrs = {
                        k: v for k, v in create_attrs.items() if v is not None
                    }
                    edu = Education.objects.create(**create_attrs)
            ResumeEducation.objects.get_or_create(
                resume_id=resume.id, education_id=edu.id
            )

        # Upsert Certifications and link
        for item in certifications_in or []:
            if not isinstance(item, dict):
                continue
            cert = None
            cid = _int_or_none(item.get("id"))
            if cid:
                cert = Certification.objects.filter(pk=cid).first()
            if cert is None:
                lookup = {
                    "issuer": item.get("issuer"),
                    "title": item.get("title"),
                    "issue_date": _parse_date(item.get("issue_date")),
                }
                lookup = {k: v for k, v in lookup.items() if v is not None}
                cert = Certification.objects.filter(**lookup).first()
                if not cert:
                    create_attrs = {
                        "issuer": item.get("issuer"),
                        "title": item.get("title"),
                        "issue_date": _parse_date(item.get("issue_date")),
                        "content": item.get("content"),
                    }
                    create_attrs = {
                        k: v for k, v in create_attrs.items() if v is not None
                    }
                    cert = Certification.objects.create(**create_attrs)
            ResumeCertification.objects.get_or_create(
                resume_id=resume.id, certification_id=cert.id
            )

        # Upsert Skills and link
        skills_in = node.get("skills") or data.get("skills") or []
        for item in skills_in or []:
            if not isinstance(item, dict):
                continue
            s_node = (item.get("data") or item) or {}
            # Resolve or create skill
            skill = None
            sid = _int_or_none(s_node.get("id"))
            if sid:
                skill = Skill.objects.filter(pk=sid).first()
            if skill is None:
                s_attrs = s_node.get("attributes") or {}
                text = s_attrs.get("text") or s_node.get("text")
                if not text:
                    continue  # ignore invalid entries
                skill, _ = Skill.objects.get_or_create(text=str(text).strip())
            # Determine 'active' (default True)
            active_val = (s_node.get("attributes") or {}).get("active")
            active_val = bool(active_val) if active_val is not None else True
            ResumeSkill.objects.get_or_create(
                resume_id=resume.id,
                skill_id=skill.id,
                defaults={"active": active_val},
            )

        # Upsert Summaries and link
        active_set = False
        for item in summaries_in or []:
            if not isinstance(item, dict):
                continue
            s_node = (item.get("data") or item) or {}
            summary = None
            sid = s_node.get("id")
            if sid is not None:
                try:
                    summary = Summary.objects.filter(pk=int(sid)).first()
                except (TypeError, ValueError):
                    summary = None
                if summary is None:
                    return Response(
                        {"errors": [{"detail": f"Invalid summary id: {sid}"}]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            else:
                s_attrs = s_node.get("attributes") or {}
                s_rels = s_node.get("relationships") or {}
                content = s_attrs.get("content")
                if not content:
                    return Response(
                        {
                            "errors": [
                                {
                                    "detail": "Summary content is required when no id is provided"
                                }
                            ]
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                # Resolve job_post if provided (accept hyphenated/underscored keys)
                jp_rel = (
                    s_rels.get("job-post")
                    or s_rels.get("job_post")
                    or s_rels.get("jobPost")
                    or s_rels.get("job-posts")
                    or s_rels.get("jobPosts")
                    or {}
                )
                jp_id = None
                if isinstance(jp_rel, dict):
                    d = jp_rel.get("data")
                    if isinstance(d, dict):
                        jp_id = d.get("id")
                try:
                    jp_id = int(jp_id) if jp_id is not None else None
                except (TypeError, ValueError):
                    return Response(
                        {
                            "errors": [
                                {
                                    "detail": "Invalid job-post ID in summary.relationships"
                                }
                            ]
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                # Resolve user_id (fallback to resume.user_id)
                user_rel = s_rels.get("user") or s_rels.get("users") or {}
                u_id = None
                if isinstance(user_rel, dict):
                    d = user_rel.get("data")
                    if isinstance(d, dict):
                        u_id = d.get("id")
                try:
                    u_id = (
                        int(u_id)
                        if u_id is not None
                        else getattr(resume, "user_id", None)
                    )
                except (TypeError, ValueError):
                    u_id = getattr(resume, "user_id", None)

                summary = Summary.objects.create(
                    job_post_id=jp_id, user_id=u_id, content=content
                )

            # Link summary to resume; mark the first one as active
            ResumeSummary.objects.get_or_create(
                resume_id=resume.id,
                summary_id=summary.id,
                defaults={"active": (not active_set)},
            )
            active_set = True
        ResumeSummary.ensure_single_active_for_resume(resume.id)

        payload = {"data": ser.to_resource(resume)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([resume], include_rels, request)
        return Response(payload, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def scores(self, request, pk=None):
        obj = Resume.objects.filter(pk=int(pk)).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [ScoreSerializer().to_resource(s) for s in obj.scores.all()]
        return Response({"data": data})

    @action(
        detail=True,
        methods=["get"],
        url_path="cover-letters",
        permission_classes=[IsAuthenticated],
    )
    def cover_letters(self, request, pk=None):
        obj = Resume.objects.filter(pk=int(pk)).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        cover_letters = list(
            CoverLetter.objects.filter(resume_id=obj.id, user_id=request.user.id)
        )
        data = [CoverLetterSerializer().to_resource(c) for c in cover_letters]
        return Response({"data": data})

    @action(detail=True, methods=["get"], url_path="job-applications")
    def applications(self, request, pk=None):
        obj = Resume.objects.filter(pk=int(pk)).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [
            JobApplicationSerializer().to_resource(a) for a in obj.applications.all()
        ]
        return Response({"data": data})

    @extend_schema(
        methods=["GET"],
        tags=["Resumes"],
        summary="List summaries for a resume",
        responses={200: _JSONAPI_LIST},
    )
    @extend_schema(
        methods=["POST"],
        tags=["Resumes"],
        summary="Create/AI-generate a summary for a resume",
        request=_JSONAPI_WRITE,
        responses={
            201: _JSONAPI_ITEM,
            400: OpenApiResponse(description="Missing job-post or invalid IDs"),
            503: OpenApiResponse(description="AI client not configured"),
        },
    )
    @action(detail=True, methods=["get", "post"])
    def summaries(self, request, pk=None):
        if request.method.lower() == "post":
            obj = Resume.objects.filter(pk=int(pk)).first()  # obj is the Resume
            if not obj:
                return Response({"errors": [{"detail": "Not found"}]}, status=404)

            data = request.data if isinstance(request.data, dict) else {}
            node = data.get("data") or {}
            attrs = node.get("attributes") or {}
            relationships = node.get("relationships") or {}

            # Accept hyphenated or underscored job-post relationship; allow null if content is provided
            job_post_rel = (
                relationships.get("job-post")
                or relationships.get("job_post")
                or relationships.get("jobPost")
                or relationships.get("job-posts")
                or relationships.get("jobPosts")
                or {}
            )
            job_post_id = None
            if isinstance(job_post_rel, dict):
                d = job_post_rel.get("data")
                if isinstance(d, dict):
                    job_post_id = d.get("id")

            job_post = None
            if job_post_id is not None:
                try:
                    job_post = JobPost.objects.filter(pk=int(job_post_id)).first()
                except (TypeError, ValueError):
                    job_post = None
                if not job_post:
                    return Response(
                        {"errors": [{"detail": "Invalid job-post ID"}]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            content = attrs.get("content")
            if content:
                summary = Summary(
                    job_post_id=job_post.id if job_post else None,
                    user_id=getattr(obj, "user_id", None),
                    content=content,
                )
                summary.save()
            else:
                if not job_post:
                    return Response(
                        {
                            "errors": [
                                {
                                    "detail": "Provide 'attributes.content' or a job-post relationship to generate content"
                                }
                            ]
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                client = get_client(required=False)
                if client is None:
                    return Response(
                        {
                            "errors": [
                                {
                                    "detail": "AI client not configured. Set OPENAI_API_KEY."
                                }
                            ]
                        },
                        status=503,
                    )

                summary_service = SummaryService(client, job=job_post, resume=obj)
                summary = summary_service.generate_summary()

            ResumeSummary.objects.filter(resume_id=obj.id).update(active=False)
            ResumeSummary.objects.get_or_create(
                resume_id=obj.id, summary_id=summary.id, defaults={"active": True}
            )
            ResumeSummary.objects.filter(
                resume_id=obj.id, summary_id=summary.id
            ).update(active=True)
            ResumeSummary.ensure_single_active_for_resume(obj.id)

            ser = SummarySerializer()
            payload = {"data": ser.to_resource(summary)}
            include_rels = self._parse_include(request)
            if include_rels:
                payload["included"] = self._build_included(
                    [summary], include_rels, request
                )
            return Response(payload, status=status.HTTP_201_CREATED)

        obj = Resume.objects.filter(pk=int(pk)).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        ser = SummarySerializer()
        # Parent context available if serializers want to customize behavior
        if hasattr(ser, "set_parent_context"):
            ser.set_parent_context("resume", obj.id, "summaries")

        _summary_ids = list(
            ResumeSummary.objects.filter(resume_id=obj.id).values_list(
                "summary_id", flat=True
            )
        )
        items = list(Summary.objects.filter(pk__in=_summary_ids))
        data = [ser.to_resource(s) for s in items]

        # Build included only when ?include=... is provided
        include_rels = self._parse_include(request)

        payload = {"data": data}
        if include_rels:
            payload["included"] = self._build_included(
                items, include_rels, request, primary_serializer=ser
            )
        return Response(payload)

    @extend_schema(
        tags=["Resumes"],
        summary="Retrieve a specific summary linked to a resume",
        responses={200: _JSONAPI_ITEM, 404: OpenApiResponse(description="Not found")},
    )
    @action(detail=True, methods=["get"], url_path=r"summaries/(?P<summary_id>\d+)")
    def summary(self, request, pk=None, summary_id=None):
        resume = Resume.objects.filter(pk=int(pk)).first()
        if not resume:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        try:
            sid = int(summary_id)
        except (TypeError, ValueError):
            return Response({"errors": [{"detail": "Invalid summary id"}]}, status=400)
        summary = Summary.objects.filter(pk=sid).first()
        if not summary:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        # Ensure the summary is associated with this resume via the link table
        link = ResumeSummary.objects.filter(
            resume_id=resume.id, summary_id=summary.id
        ).first()
        if not link:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = SummarySerializer()
        if hasattr(ser, "set_parent_context"):
            ser.set_parent_context("resume", resume.id, "summaries")

        # Primary data
        items = [summary]
        data = ser.to_resource(summary)

        # Build included only when ?include=... is provided, using Summary serializer
        include_rels = self._parse_include(request)

        payload = {"data": data}
        if include_rels:
            payload["included"] = self._build_included(
                items, include_rels, request, primary_serializer=ser
            )
        return Response(payload)

    @extend_schema(
        tags=["Resumes"],
        summary="List experiences for a resume",
        responses={200: _JSONAPI_LIST},
    )
    @action(detail=True, methods=["get"])
    def experiences(self, request, pk=None):
        obj = Resume.objects.filter(pk=int(pk)).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = ExperienceSerializer()
        ser.set_parent_context("resume", obj.id, "experiences")
        _exp_ids = list(
            ResumeExperience.objects.filter(resume_id=obj.id)
            .order_by("order")
            .values_list("experience_id", flat=True)
        )
        _exp_map = {e.id: e for e in Experience.objects.filter(pk__in=_exp_ids)}
        items = [_exp_map[eid] for eid in _exp_ids if eid in _exp_map]
        data = [ser.to_resource(e) for e in items]

        # Build included only when ?include=... is provided
        include_rels = self._parse_include(request)

        payload = {"data": data}
        if include_rels:
            payload["included"] = self._build_included(
                items, include_rels, request, primary_serializer=ser
            )
        return Response(payload)

    @extend_schema(
        tags=["Resumes"],
        summary="List educations for a resume",
        responses={200: _JSONAPI_LIST},
    )
    @action(detail=True, methods=["get"])
    def educations(self, request, pk=None):
        obj = Resume.objects.filter(pk=int(pk)).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = EducationSerializer()
        ser.set_parent_context("resume", obj.id, "educations")
        _edu_ids = list(
            ResumeEducation.objects.filter(resume_id=obj.id).values_list(
                "education_id", flat=True
            )
        )
        data = [ser.to_resource(e) for e in Education.objects.filter(pk__in=_edu_ids)]
        return Response({"data": data})

    @extend_schema(
        tags=["Resumes"],
        summary="List skills for a resume",
        responses={200: _JSONAPI_LIST},
    )
    @action(detail=True, methods=["get"])
    def skills(self, request, pk=None):
        obj = Resume.objects.filter(pk=int(pk)).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = SkillSerializer()
        ser.set_parent_context("resume", obj.id, "skills")
        skill_ids = list(
            ResumeSkill.objects.filter(resume_id=obj.id).values_list(
                "skill_id", flat=True
            )
        )
        items = list(Skill.objects.filter(pk__in=skill_ids))
        data = [ser.to_resource(s) for s in items]

        # Build included only when ?include=... is provided
        include_rels = self._parse_include(request)

        payload = {"data": data}
        if include_rels:
            payload["included"] = self._build_included(
                items, include_rels, request, primary_serializer=ser
            )
        return Response(payload)

    @extend_schema(
        tags=["Resumes"],
        summary="Export a resume as DOCX or Markdown",
        parameters=[
            OpenApiParameter(
                "format",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                required=False,
                description="'docx' (default) or 'md'",
            ),
            OpenApiParameter(
                "template_path",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                required=False,
                description="Path to a custom DOCX template (docx only)",
            ),
        ],
        responses={
            200: OpenApiResponse(
                description="File download (application/vnd.openxmlformats-officedocument.wordprocessingml.document or text/markdown)"
            ),
            404: OpenApiResponse(description="Not found"),
        },
    )
    @action(detail=True, methods=["get"], url_path="export")
    def export(self, request, pk=None):
        obj = Resume.objects.filter(pk=int(pk)).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        template_path_param = request.query_params.get("template_path")

        # DOCX is the only format this action serves. For markdown, the
        # dedicated /api/v1/resumes/<pk>/markdown/ route is authoritative —
        # ?format=md here is not wired because DRF intercepts the `format`
        # query param for renderer selection and 404s before the view runs.
        #
        # Rendering uses python-docx directly against a styles-only base
        # (templates/resume_styles.docx by default), no jinja in Word, no
        # docxtpl tag-splitting failures. ``template_path`` query param
        # lets callers point at a different styles base (per-theme branding).
        import os
        from django.conf import settings
        from job_hunting.lib.services.resume_docx_render import (
            render_docx as render_resume_docx,
        )

        base_path = template_path_param or os.path.join(
            settings.BASE_DIR, "templates", "resume_styles.docx"
        )
        if not os.path.exists(base_path):
            # Fall back to no-base rendering if the styles file isn't
            # deployed yet — better a plainly-styled docx than a 500.
            base_path = None

        try:
            data = render_resume_docx(obj, base_template_path=base_path)
        except ImportError:
            return Response(
                {
                    "errors": [
                        {"detail": "DOCX export requires 'python-docx' to be installed"}
                    ]
                },
                status=status.HTTP_501_NOT_IMPLEMENTED,
            )
        except Exception as e:
            return Response(
                {"errors": [{"detail": str(e)}]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Generate filename
        import re

        filename_parts = ["resume", str(obj.id)]
        try:
            if getattr(obj, "user", None) and getattr(obj.user, "name", None):
                name = str(obj.user.name)
                sanitized_name = re.sub(r"[^A-Za-z0-9_-]+", "-", name)
                if sanitized_name:
                    filename_parts.append(sanitized_name)
            if getattr(obj, "title", None):
                title = str(obj.title)
                sanitized_title = re.sub(r"[^A-Za-z0-9_-]+", "-", title)
                if sanitized_title:
                    filename_parts.append(sanitized_title)
        except Exception:
            pass
        filename = "-".join([p for p in filename_parts if p]) + ".docx"

        response = HttpResponse(
            data,
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    @extend_schema(
        tags=["Resumes"],
        summary="Clone a resume and all its relationships",
        responses={
            201: _JSONAPI_ITEM,
            404: OpenApiResponse(description="Not found"),
        },
    )
    @action(detail=True, methods=["post"], url_path="clone")
    def clone(self, request, pk=None):
        obj = Resume.objects.filter(pk=int(pk)).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        new_resume = Resume.objects.create(
            user_id=obj.user_id,
            file_path=obj.file_path,
            title=f"{obj.title} (clone)" if obj.title else None,
            name=f"{obj.name} (clone)" if obj.name else None,
            notes=obj.notes,
            favorite=obj.favorite,
        )

        for rs in ResumeSkill.objects.filter(resume_id=obj.id):
            ResumeSkill.objects.create(resume_id=new_resume.id, skill_id=rs.skill_id, active=rs.active)

        for re in ResumeExperience.objects.filter(resume_id=obj.id):
            ResumeExperience.objects.create(resume_id=new_resume.id, experience_id=re.experience_id, order=re.order)

        for rp in ResumeProject.objects.filter(resume_id=obj.id):
            ResumeProject.objects.create(resume_id=new_resume.id, project_id=rp.project_id, order=rp.order)

        for rc in ResumeCertification.objects.filter(resume_id=obj.id):
            ResumeCertification.objects.create(
                resume_id=new_resume.id,
                certification_id=rc.certification_id,
                issuer=rc.issuer,
                title=rc.title,
                issue_date=rc.issue_date,
                content=rc.content,
            )

        for red in ResumeEducation.objects.filter(resume_id=obj.id):
            ResumeEducation.objects.create(
                resume_id=new_resume.id,
                education_id=red.education_id,
                institution=red.institution,
                degree=red.degree,
                issue_date=red.issue_date,
                content=red.content,
            )

        for rsm in ResumeSummary.objects.filter(resume_id=obj.id):
            ResumeSummary.objects.create(resume_id=new_resume.id, summary_id=rsm.summary_id, active=rsm.active)

        ser = self.get_serializer()
        payload = {"data": ser.to_resource(new_resume)}
        ser_rels = list(getattr(ser, "relationships", {}).keys())
        include_rels = self._parse_include(request) or ser_rels
        if include_rels:
            payload["included"] = self._build_included([new_resume], include_rels, request)
        return Response(payload, status=status.HTTP_201_CREATED)

    @extend_schema(
        tags=["Resumes"],
        summary="Ingest a resume from an uploaded DOCX or PDF file",
        request=inline_serializer(
            name="IngestResumeRequest",
            fields={
                "file": drf_serializers.FileField(
                    help_text="DOCX or PDF resume file (multipart/form-data)"
                )
            },
        ),
        responses={
            201: _JSONAPI_ITEM,
            400: OpenApiResponse(description="No file provided or unsupported format"),
        },
    )
    @action(detail=False, methods=["post"], url_path="ingest")
    def ingest(self, request):
        """
        Ingest a resume from an uploaded docx file.

        Creates a placeholder resume with status='pending', processes in background,
        and returns HTTP 202. Frontend polls until status is 'completed' or 'failed'.
        """
        if "file" not in request.FILES:
            return Response(
                {"errors": [{"detail": "No file uploaded. Expected 'file' field with docx content."}]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        uploaded_file = request.FILES["file"]

        lower_name = uploaded_file.name.lower()
        if not (lower_name.endswith(".docx") or lower_name.endswith(".pdf")):
            return Response(
                {"errors": [{"detail": "Only .docx and .pdf files are supported"}]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        file_blob = uploaded_file.read()
        resume_name = uploaded_file.name

        # Derive a display name from the filename
        base_name = resume_name
        if lower_name.endswith(".docx"):
            base_name = base_name[:-5]
        elif lower_name.endswith(".pdf"):
            base_name = base_name[:-4]
        derived_name = base_name.strip()[:100] or "Imported Resume"

        # Create placeholder resume immediately. file_path records the
        # original filename (as the user uploaded it) for reference —
        # the server doesn't keep the uploaded blob on disk, so this is
        # purely a human-readable "where did this come from" marker.
        resume = Resume.objects.create(
            user_id=request.user.id,
            name=derived_name,
            file_path=resume_name,
            status="pending",
        )

        resume_id = resume.id
        user_id = request.user.id

        def _ingest():
            import django
            django.db.close_old_connections()
            User = get_user_model()
            try:
                ingest_service = IngestResume(
                    user=User.objects.get(pk=user_id),
                    resume=file_blob,
                    resume_name=resume_name,
                    agent=None,
                    db_resume=Resume.objects.get(pk=resume_id),
                )
                ingest_service.process()

                r = Resume.objects.filter(pk=resume_id).first()
                if r:
                    if not r.title:
                        r.title = derived_name
                    r.status = "completed"
                    r.save()
            except Exception:
                logger.exception("Resume ingest failed for resume_id=%s", resume_id)
                Resume.objects.filter(pk=resume_id).update(status="failed")

        import threading

        def _ingest_with_timeout():
            t = threading.Thread(target=_ingest, daemon=True)
            t.start()
            t.join(timeout=300)  # 5 minute ceiling
            if t.is_alive():
                logger.error("Resume ingest timed out for resume_id=%s", resume_id)
                Resume.objects.filter(pk=resume_id).update(status="failed")

        threading.Thread(target=_ingest_with_timeout, daemon=True).start()

        ser = self.get_serializer()
        payload = {"data": ser.to_resource(resume)}
        return Response(payload, status=status.HTTP_202_ACCEPTED)

