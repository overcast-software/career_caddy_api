import asyncio
import re
import dateparser
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.parsers import JSONParser
from .parsers import VndApiJSONParser
from job_hunting.lib.scoring.job_scorer import JobScorer
from job_hunting.lib.ai_client import ai_client
from job_hunting.lib.services.summary_service import SummaryService
from job_hunting.lib.services.cover_letter_service import CoverLetterService
from job_hunting.lib.services.generic_service import GenericService
from job_hunting.lib.services.db_export_service import DbExportService

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
    ResumeEducation,
    ResumeCertification,
    ResumeSummaries,
    ResumeExperience,
)
from .serializers import (
    UserSerializer,
    ResumeSerializer,
    ScoreSerializer,
    JobPostSerializer,
    ScrapeSerializer,
    CompanySerializer,
    CoverLetterSerializer,
    ApplicationSerializer,
    SummarySerializer,
    ExperienceSerializer,
    EducationSerializer,
    CertificationSerializer,
    DescriptionSerializer,
    TYPE_TO_SERIALIZER,
    _parse_date,
)


class BaseSAViewSet(viewsets.ViewSet):
    permission_classes = [AllowAny]
    parser_classes = [VndApiJSONParser, JSONParser]
    model = None
    serializer_class = None

    def get_session(self):
        return self.model.get_session()

    def get_serializer(self):
        return self.serializer_class()

    def _parse_include(self, request):
        raw = []
        inc = request.query_params.get("include")
        incs = request.query_params.get("includes")
        if inc:
            raw.append(str(inc))
        if incs and incs != inc:
            raw.append(str(incs))
        if not raw:
            return []
        parts = []
        for chunk in raw:
            parts.extend([s.strip() for s in chunk.split(",") if s and s.strip()])
        # de-duplicate while preserving order
        seen = set()
        out = []
        for p in parts:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return out

    def _build_included(self, objs, include_rels):
        included = []
        seen = set()  # (type, id)
        primary_ser = self.get_serializer()
        rel_keys = set(getattr(primary_ser, "relationships", {}).keys())

        def _normalize_rel(name: str) -> str:
            if name in rel_keys:
                return name
            # Try simple plural forms
            if not name.endswith("s") and f"{name}s" in rel_keys:
                return f"{name}s"
            if (
                name.endswith("y")
                and not name.endswith(("ay", "ey", "iy", "oy", "uy"))
                and f"{name[:-1]}ies" in rel_keys
            ):
                return f"{name[:-1]}ies"
            return name

        for obj in objs:
            for rel in include_rels:
                rel = _normalize_rel(rel)
                rel_type, targets = primary_ser.get_related(obj, rel)
                if not rel_type:
                    continue
                ser_cls = TYPE_TO_SERIALIZER.get(rel_type)
                if not ser_cls:
                    continue
                rel_ser = ser_cls()
                # Provide parent context so serializers can customize included resources
                if hasattr(rel_ser, "set_parent_context"):
                    rel_ser.set_parent_context(primary_ser.type, obj.id, rel)
                for t in targets:
                    key = (rel_type, str(t.id))
                    if key in seen:
                        continue
                    seen.add(key)
                    included.append(rel_ser.to_resource(t))

                    # Auto-include children of experience (descriptions and company)
                    if rel_type == "experience":
                        exp_child_ser = ExperienceSerializer()
                        if hasattr(exp_child_ser, "set_parent_context"):
                            exp_child_ser.set_parent_context("experience", t.id, None)
                        for child_rel in ("descriptions", "company"):
                            c_type, c_targets = exp_child_ser.get_related(t, child_rel)
                            if not c_type:
                                continue
                            c_ser_cls = TYPE_TO_SERIALIZER.get(c_type)
                            if not c_ser_cls:
                                continue
                            c_ser = c_ser_cls()
                            if hasattr(c_ser, "set_parent_context"):
                                c_ser.set_parent_context("experience", t.id, child_rel)
                            for c in c_targets:
                                c_key = (c_type, str(c.id))
                                if c_key in seen:
                                    continue
                                seen.add(c_key)
                                included.append(c_ser.to_resource(c))
        return included

    def paginate(self, items):
        try:
            page_number = int(self.request.query_params.get("page[number]", 1))
        except Exception:
            page_number = 1
        try:
            page_size = int(self.request.query_params.get("page[size]", 50))
        except Exception:
            page_size = 50
        start = (page_number - 1) * page_size
        end = start + page_size
        return items[start:end]

    def list(self, request):
        session = self.get_session()
        items = session.query(self.model).all()
        items = self.paginate(items)
        ser = self.get_serializer()
        data = [ser.to_resource(o) for o in items]
        payload = {"data": data}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included(items, include_rels)
        return Response(payload)

    def retrieve(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer()
        payload = {"data": ser.to_resource(obj)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([obj], include_rels)
        return Response(payload)

    def create(self, request):
        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response({"errors": [{"detail": str(e)}]}, status=400)
        obj = self.model(**attrs)
        session = self.get_session()
        session.add(obj)
        session.commit()
        return Response({"data": ser.to_resource(obj)}, status=status.HTTP_201_CREATED)

    def update(self, request, pk=None):
        return self._upsert(request, pk, partial=False)

    def partial_update(self, request, pk=None):
        return self._upsert(request, pk, partial=True)

    def _upsert(self, request, pk, partial=False):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response({"errors": [{"detail": str(e)}]}, status=400)
        for k, v in attrs.items():
            setattr(obj, k, v)
        session = self.get_session()
        session.add(obj)
        session.commit()
        return Response({"data": ser.to_resource(obj)})

    def destroy(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response(status=204)
        session = self.get_session()
        session.delete(obj)
        session.commit()
        return Response(status=204)

    # JSON:API relationships linkage endpoint:
    # GET /<type>/{id}/relationships/<rel-name>
    @action(detail=True, methods=["get"], url_path=r"relationships/(?P<rel>[^/]+)")
    def relationships(self, request, pk=None, rel=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer()
        cfg = (
            ser.relationships.get(rel)
            if ser and hasattr(ser, "relationships")
            else None
        )
        if not cfg:
            return Response(
                {"errors": [{"detail": "Relationship not found"}]}, status=404
            )
        rel_type = cfg["type"]
        uselist = cfg.get("uselist", True)
        target = getattr(obj, cfg["attr"], None)
        if uselist:
            data = [{"type": rel_type, "id": str(i.id)} for i in (target or [])]
        else:
            data = {"type": rel_type, "id": str(target.id)} if target else None
        return Response({"data": data})


class SummaryViewSet(BaseSAViewSet):
    model = Summary
    serializer_class = SummarySerializer

    def create(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") or {}
        attrs = node.get("attributes") or {}
        relationships = node.get("relationships") or {}

        def _first_id(node):
            if isinstance(node, dict):
                d = node.get("data")
            else:
                d = None
            if isinstance(d, dict) and "id" in d:
                return d["id"]
            if isinstance(d, list) and d:
                first = d[0]
                if isinstance(first, dict) and "id" in first:
                    return first["id"]
            return None

        def _rel_id(*keys):
            for k in keys:
                v = relationships.get(k)
                if v is not None:
                    rid = _first_id(v)
                    if rid is not None:
                        return rid
            return None

        # Accept both hyphenated and underscored keys; allow nullable job_post
        resume_id = _rel_id("resume", "resumes")
        job_post_id = _rel_id(
            "job-post", "job_post", "jobPost", "job-posts", "jobPosts"
        )
        user_id = _rel_id("user", "users")

        if resume_id is None:
            return Response(
                {"errors": [{"detail": "Missing required relationship: resume"}]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            resume = Resume.get(int(resume_id))
        except (TypeError, ValueError):
            resume = None
        if not resume:
            return Response(
                {"errors": [{"detail": "Invalid resume ID"}]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        job_post = None
        if job_post_id is not None:
            try:
                job_post = JobPost.get(int(job_post_id))
            except (TypeError, ValueError):
                job_post = None
            if not job_post:
                return Response(
                    {"errors": [{"detail": "Invalid job-post ID"}]},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Resolve user_id; default to resume.user_id if not provided or invalid
        try:
            user_id = int(user_id) if user_id is not None else None
        except (TypeError, ValueError):
            user_id = None
        if user_id is None:
            user_id = getattr(resume, "user_id", None)

        content = attrs.get("content")

        if content:
            summary = Summary(
                job_post_id=job_post.id if job_post else None,
                user_id=user_id,
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
            summary_service = SummaryService(ai_client, job=job_post, resume=resume)
            summary = summary_service.generate_summary()

        session = self.get_session()
        # Deactivate existing links, then create new active link
        session.query(ResumeSummaries).filter_by(resume_id=resume.id).update(
            {ResumeSummaries.active: False}
        )
        session.add(
            ResumeSummaries(resume_id=resume.id, summary_id=summary.id, active=True)
        )
        session.commit()

        ser = self.get_serializer()
        payload = {"data": ser.to_resource(summary)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([summary], include_rels)
        return Response(payload, status=status.HTTP_201_CREATED)


class UserViewSet(BaseSAViewSet):
    model = User
    serializer_class = UserSerializer

    @action(detail=True, methods=["get"])
    def resumes(self, request, pk=None):
        user = self.model.get(int(pk))
        if not user:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [ResumeSerializer().to_resource(r) for r in (user.resumes or [])]
        return Response({"data": data})

    @action(detail=True, methods=["get"])
    def scores(self, request, pk=None):
        user = self.model.get(int(pk))
        if not user:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [ScoreSerializer().to_resource(s) for s in (user.scores or [])]
        return Response({"data": data})

    @action(detail=True, methods=["get"], url_path="cover-letters")
    def cover_letters(self, request, pk=None):
        user = self.model.get(int(pk))
        if not user:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [
            CoverLetterSerializer().to_resource(c) for c in (user.cover_letters or [])
        ]
        return Response({"data": data})

    @action(detail=True, methods=["get"])
    def applications(self, request, pk=None):
        user = self.model.get(int(pk))
        if not user:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [
            ApplicationSerializer().to_resource(a) for a in (user.applications or [])
        ]
        return Response({"data": data})

    @action(detail=True, methods=["get"])
    def summaries(self, request, pk=None):
        user = self.model.get(int(pk))
        if not user:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [SummarySerializer().to_resource(s) for s in (user.summaries or [])]
        return Response({"data": data})


class ResumeViewSet(BaseSAViewSet):
    model = Resume
    serializer_class = ResumeSerializer

    def list(self, request):
        session = self.get_session()
        items = session.query(self.model).all()
        items = self.paginate(items)
        ser = self.get_serializer()
        data = [ser.to_resource(o) for o in items]
        payload = {"data": data}
        # Default to include all resume relationships if none specified
        ser_rels = list(getattr(ser, "relationships", {}).keys())
        include_rels = self._parse_include(request) or ser_rels
        if include_rels:
            payload["included"] = self._build_included(items, include_rels)
        return Response(payload)

    def retrieve(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer()
        payload = {"data": ser.to_resource(obj)}
        # Default to include all resume relationships if none specified
        ser_rels = list(getattr(ser, "relationships", {}).keys())
        include_rels = self._parse_include(request) or ser_rels
        if include_rels:
            payload["included"] = self._build_included([obj], include_rels)
        return Response(payload)

    def _upsert(self, request, pk, partial=False):
        session = self.get_session()
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response({"errors": [{"detail": str(e)}]}, status=400)
        for k, v in attrs.items():
            setattr(obj, k, v)
        session.add(obj)
        session.commit()

        # Optional: update active summary content if attributes.summary is provided
        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") or {}
        attrs_node = node.get("attributes") or {}
        incoming_summary = attrs_node.get("summary")
        if isinstance(incoming_summary, str):
            new_content = incoming_summary.strip()
            # Find active link
            active_link = (
                session.query(ResumeSummaries)
                .filter_by(resume_id=obj.id, active=True)
                .first()
            )
            if active_link:
                sm = Summary.get(active_link.summary_id)
                if sm and (sm.content or "") != new_content:
                    sm.content = new_content
                    session.add(sm)
                    session.commit()
                # Ensure only this one is active
                session.query(ResumeSummaries).filter(
                    ResumeSummaries.resume_id == obj.id,
                    ResumeSummaries.id != active_link.id,
                ).update({ResumeSummaries.active: False})
                active_link.active = True
                session.add(active_link)
                session.commit()
            else:
                # No active summary; create one and activate it
                sm = Summary(
                    job_post_id=None,
                    user_id=getattr(obj, "user_id", None),
                    content=new_content,
                )
                sm.save()
                session.query(ResumeSummaries).filter_by(resume_id=obj.id).update(
                    {ResumeSummaries.active: False}
                )
                session.add(
                    ResumeSummaries(resume_id=obj.id, summary_id=sm.id, active=True)
                )
                session.commit()

        payload = {"data": ser.to_resource(obj)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([obj], include_rels)
        return Response(payload)

    def create(self, request):
        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response(
                {"errors": [{"detail": str(e)}]}, status=status.HTTP_400_BAD_REQUEST
            )

        session = self.get_session()

        # Create the resume
        resume = Resume(**attrs)
        session.add(resume)
        session.commit()  # ensure resume.id

        # Extract potential child arrays from JSON:API attributes or top-level convenience keys
        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") or {}
        attrs_node = node.get("attributes") or {}
        experiences_in = node.get("experiences") or data.get("experiences") or []
        educations_in = node.get("educations") or data.get("educations") or []
        certifications_in = (
            node.get("certifications") or data.get("certifications") or []
        )

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
                exp = Experience.get(eid)

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
                        dd = Description.get(int(d["id"]))
                        if dd and getattr(dd, "content", None):
                            incoming_lines.append(dd.content.strip())

            # Parse dates via dateparser
            s_date = _dp(item.get("start_date"))
            e_date = _dp(item.get("end_date"))

            if exp is None:
                # Try to find an existing experience with matching scalars and identical description list
                candidates = (
                    session.query(Experience)
                    .filter_by(
                        company_id=company_id,
                        title=item.get("title"),
                        location=item.get("location"),
                        summary=(item.get("summary") or ""),
                        start_date=s_date,
                        end_date=e_date,
                    )
                    .all()
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
                    exp = Experience(
                        company_id=company_id,
                        title=item.get("title"),
                        location=item.get("location"),
                        summary=(item.get("summary") or ""),
                        start_date=s_date,
                        end_date=e_date,
                        content=item.get("content"),
                    )
                    session.add(exp)
                    session.commit()
                    # Link descriptions in order, creating Description rows as needed
                    session.query(ExperienceDescription).filter_by(
                        experience_id=exp.id
                    ).delete()
                    session.commit()
                    for idx, line in enumerate(incoming_lines or []):
                        if not line:
                            continue
                        desc, _ = Description.first_or_create(
                            session=session, content=line
                        )
                        session.add(
                            ExperienceDescription(
                                experience_id=exp.id,
                                description_id=desc.id,
                                order=idx,
                            )
                        )
                    session.commit()

            # Join resume_experience (avoid duplicates)
            ResumeExperience.first_or_create(
                session=session,
                resume_id=resume.id,
                experience_id=exp.id,
            )
            session.commit()

            # Nested descriptions for this experience
            for d in item.get("descriptions") or []:
                if not isinstance(d, dict):
                    continue
                desc = None
                did = _int_or_none(d.get("id"))
                if did:
                    desc = Description.get(did)
                if desc is None:
                    content = d.get("content")
                    if not content:
                        continue
                    desc, _ = Description.first_or_create(
                        session=session, content=content
                    )
                # Link with optional order
                order = d.get("order")
                if order is None and isinstance(d.get("meta"), dict):
                    order = d["meta"].get("order")
                try:
                    order = int(order) if order is not None else 0
                except (TypeError, ValueError):
                    order = 0
                ExperienceDescription.first_or_create(
                    session=session,
                    experience_id=exp.id,
                    description_id=desc.id,
                    defaults={"order": order},
                )
                session.commit()

        # Upsert Educations and link
        for item in educations_in or []:
            if not isinstance(item, dict):
                continue
            edu = None
            eid = _int_or_none(item.get("id"))
            if eid:
                edu = Education.get(eid)
            if edu is None:
                lookup = {
                    "institution": item.get("institution"),
                    "degree": item.get("degree"),
                    "major": item.get("major"),
                    "minor": item.get("minor"),
                    "issue_date": _parse_date(item.get("issue_date")),
                }
                # For lookup, None values ignored; but include issue_date only if parsed
                lookup = {k: v for k, v in lookup.items() if v is not None}
                edu = session.query(Education).filter_by(**lookup).first()
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
                    edu = Education(**create_attrs)
                    session.add(edu)
                    session.commit()
            session.add(ResumeEducation(resume_id=resume.id, education_id=edu.id))
            session.commit()

        # Upsert Certifications and link
        for item in certifications_in or []:
            if not isinstance(item, dict):
                continue
            cert = None
            cid = _int_or_none(item.get("id"))
            if cid:
                cert = Certification.get(cid)
            if cert is None:
                lookup = {
                    "issuer": item.get("issuer"),
                    "title": item.get("title"),
                    "issue_date": _parse_date(item.get("issue_date")),
                }
                lookup = {k: v for k, v in lookup.items() if v is not None}
                cert = session.query(Certification).filter_by(**lookup).first()
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
                    cert = Certification(**create_attrs)
                    session.add(cert)
                    session.commit()
            session.add(
                ResumeCertification(resume_id=resume.id, certification_id=cert.id)
            )
            session.commit()

        # Refresh relationships so response includes all links
        try:
            session.expire(resume, ["experiences", "educations", "certifications"])
        except Exception:
            pass

        payload = {"data": ser.to_resource(resume)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([resume], include_rels)
        return Response(payload, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def scores(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [ScoreSerializer().to_resource(s) for s in (obj.scores or [])]
        return Response({"data": data})

    @action(detail=True, methods=["get"], url_path="cover-letters")
    def cover_letters(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [
            CoverLetterSerializer().to_resource(c) for c in (obj.cover_letters or [])
        ]
        return Response({"data": data})

    @action(detail=True, methods=["get"])
    def applications(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [
            ApplicationSerializer().to_resource(a) for a in (obj.applications or [])
        ]
        return Response({"data": data})

    @action(detail=True, methods=["get", "post"])
    def summaries(self, request, pk=None):
        if request.method.lower() == "post":
            obj = self.model.get(int(pk))  # obj is the Resume
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
                    job_post = JobPost.get(int(job_post_id))
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
                summary_service = SummaryService(ai_client, job=job_post, resume=obj)
                summary = summary_service.generate_summary()

            session = self.get_session()
            session.query(ResumeSummaries).filter_by(resume_id=obj.id).update(
                {ResumeSummaries.active: False}
            )
            session.add(
                ResumeSummaries(resume_id=obj.id, summary_id=summary.id, active=True)
            )
            session.commit()

            ser = SummarySerializer()
            payload = {"data": ser.to_resource(summary)}
            include_rels = self._parse_include(request)
            if include_rels:
                payload["included"] = self._build_included([summary], include_rels)
            return Response(payload, status=status.HTTP_201_CREATED)

        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        ser = SummarySerializer()
        # Parent context available if serializers want to customize behavior
        if hasattr(ser, "set_parent_context"):
            ser.set_parent_context("resume", obj.id, "summaries")

        items = list(obj.summaries or [])
        data = [ser.to_resource(s) for s in items]

        # Build included only when ?include=... is provided
        include_rels = self._parse_include(request)

        included = []
        if include_rels:
            seen = set()
            rel_keys = set(getattr(ser, "relationships", {}).keys())

            def _normalize_rel(name: str) -> str:
                if name in rel_keys:
                    return name
                if not name.endswith("s") and f"{name}s" in rel_keys:
                    return f"{name}s"
                if (
                    name.endswith("y")
                    and not name.endswith(("ay", "ey", "iy", "oy", "uy"))
                    and f"{name[:-1]}ies" in rel_keys
                ):
                    return f"{name[:-1]}ies"
                return name

            for sm in items:
                for rel in include_rels:
                    rel = _normalize_rel(rel)
                    rel_type, targets = ser.get_related(sm, rel)
                    if not rel_type:
                        continue
                    ser_cls = TYPE_TO_SERIALIZER.get(rel_type)
                    if not ser_cls:
                        continue
                    rel_ser = ser_cls()
                    if hasattr(rel_ser, "set_parent_context"):
                        rel_ser.set_parent_context(ser.type, sm.id, rel)
                    for t in targets:
                        key = (rel_type, str(t.id))
                        if key in seen:
                            continue
                        seen.add(key)
                        included.append(rel_ser.to_resource(t))

        payload = {"data": data}
        if included:
            payload["included"] = included
        return Response(payload)

    @action(detail=True, methods=["get"], url_path=r"summaries/(?P<summary_id>\d+)")
    def summary(self, request, pk=None, summary_id=None):
        session = self.get_session()
        resume = self.model.get(int(pk))
        if not resume:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        try:
            sid = int(summary_id)
        except (TypeError, ValueError):
            return Response({"errors": [{"detail": "Invalid summary id"}]}, status=400)
        summary = Summary.get(sid)
        if not summary:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        # Ensure the summary is associated with this resume via the link table
        link = (
            session.query(ResumeSummaries)
            .filter_by(resume_id=resume.id, summary_id=summary.id)
            .first()
        )
        if not link:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = SummarySerializer()
        if hasattr(ser, "set_parent_context"):
            ser.set_parent_context("resume", resume.id, "summaries")
        payload = {"data": ser.to_resource(summary)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([summary], include_rels)
        return Response(payload)

    @action(detail=True, methods=["get"])
    def experiences(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = ExperienceSerializer()
        ser.set_parent_context("resume", obj.id, "experiences")
        items = list(obj.experiences or [])
        data = [ser.to_resource(e) for e in items]

        # Build included only when ?include=... is provided
        include_rels = self._parse_include(request)

        included = []
        if include_rels:
            seen = set()
            rel_keys = set(getattr(ser, "relationships", {}).keys())

            def _normalize_rel(name: str) -> str:
                if name in rel_keys:
                    return name
                if not name.endswith("s") and f"{name}s" in rel_keys:
                    return f"{name}s"
                if (
                    name.endswith("y")
                    and not name.endswith(("ay", "ey", "iy", "oy", "uy"))
                    and f"{name[:-1]}ies" in rel_keys
                ):
                    return f"{name[:-1]}ies"
                return name

            for exp in items:
                for rel in include_rels:
                    rel = _normalize_rel(rel)
                    rel_type, targets = ser.get_related(exp, rel)
                    if not rel_type:
                        continue
                    ser_cls = TYPE_TO_SERIALIZER.get(rel_type)
                    if not ser_cls:
                        continue
                    rel_ser = ser_cls()
                    if hasattr(rel_ser, "set_parent_context"):
                        rel_ser.set_parent_context(ser.type, exp.id, rel)
                    for t in targets:
                        key = (rel_type, str(t.id))
                        if key in seen:
                            continue
                        seen.add(key)
                        included.append(rel_ser.to_resource(t))

        payload = {"data": data}
        if included:
            payload["included"] = included
        return Response(payload)

    @action(detail=True, methods=["get"])
    def educations(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = EducationSerializer()
        ser.set_parent_context("resume", obj.id, "educations")
        data = [ser.to_resource(e) for e in (obj.educations or [])]
        return Response({"data": data})


class ScoreViewSet(BaseSAViewSet):
    model = Score
    serializer_class = ScoreSerializer

    def create(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        myJobScorer = JobScorer(ai_client)

        relationships = (data.get("data") or {}).get("relationships") or {}

        def _first_id(node):
            if isinstance(node, dict):
                data = node.get("data")
            else:
                data = None
            if isinstance(data, dict) and "id" in data:
                return data["id"]
            if isinstance(data, list) and data:
                first = data[0]
                if isinstance(first, dict) and "id" in first:
                    return first["id"]
            return None

        def _rel_id(*keys):
            for k in keys:
                val = relationships.get(k)
                if val is not None:
                    rid = _first_id(val)
                    if rid is not None:
                        return rid
            return None

        job_post_id = _rel_id(
            "job-post", "job_post", "jobPost", "job-posts", "jobPosts"
        )
        user_id = _rel_id("user", "users")
        resume_id = _rel_id("resume", "resumes")

        if job_post_id is None or user_id is None or resume_id is None:
            return Response(
                {
                    "errors": [
                        {
                            "detail": "Missing required relationships: user, job-post, resume"
                        }
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        job_post_id = int(job_post_id)
        user_id = int(user_id)
        resume_id = int(resume_id)

        jp = JobPost.get(job_post_id)
        resume = Resume.get(resume_id)

        # Export resume to markdown for improved scoring context
        exporter = DbExportService()
        resume_markdown = exporter.resume_markdown_export(resume)

        myScore, is_created = Score.first_or_initialize(
            job_post_id=job_post_id, resume_id=resume_id, user_id=user_id
        )

        evaluation = myJobScorer.score_job_match(jp.description, resume_markdown)
        score_value, explanation = self._parse_eval(evaluation)
        myScore.explanation = explanation
        myScore.score = score_value
        myScore.save()

        ser = self.get_serializer()
        payload = {"data": ser.to_resource(myScore)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([myScore], include_rels)
        return Response(payload, status=status.HTTP_201_CREATED)

    def _parse_eval(self, e):
        if isinstance(e, dict):
            s = e.get("score")
            expl = e.get("explanation") or e.get("evaluation")
            if isinstance(expl, dict):
                expl = expl.get("text") or str(expl)
            if s is not None and expl:
                return int(s), str(expl)
        text = str(e)
        m_score = re.search(r"(?i)\**score\**\s*[:\-]\s*(\d{1,3})", text)
        m_expl = re.search(
            r"(?i)\**(explanation|evaluation)\**\s*[:\-]\s*(.+)", text, re.DOTALL
        )
        s_val = int(m_score.group(1)) if m_score else None
        expl = m_expl.group(2).strip() if m_expl else text
        return s_val, expl


class JobPostViewSet(BaseSAViewSet):
    model = JobPost
    serializer_class = JobPostSerializer

    @action(detail=True, methods=["get"])
    def scores(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [ScoreSerializer().to_resource(s) for s in (obj.scores or [])]
        return Response({"data": data})

    @action(detail=True, methods=["get"])
    def scrapes(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [ScrapeSerializer().to_resource(s) for s in (obj.scrapes or [])]
        return Response({"data": data})

    @action(detail=True, methods=["get"], url_path="cover-letters")
    def cover_letters(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [
            CoverLetterSerializer().to_resource(c) for c in (obj.cover_letters or [])
        ]
        return Response({"data": data})

    @action(detail=True, methods=["get"])
    def applications(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [
            ApplicationSerializer().to_resource(a) for a in (obj.applications or [])
        ]
        return Response({"data": data})

    @action(detail=True, methods=["get", "post"])
    def summaries(self, request, pk=None):
        if request.method.lower() == "post":
            obj = self.model.get(int(pk))  # obj is the JobPost
            if not obj:
                return Response({"errors": [{"detail": "Not found"}]}, status=404)

            data = request.data if isinstance(request.data, dict) else {}
            node = data.get("data") or {}
            attrs = node.get("attributes") or {}
            relationships = node.get("relationships") or {}

            # Accept "resume"/"resumes" for resume relationship
            resume_rel = (
                relationships.get("resumes") or relationships.get("resume") or {}
            )
            resume_id = None
            if isinstance(resume_rel, dict):
                d = resume_rel.get("data")
                if isinstance(d, dict):
                    resume_id = d.get("id")

            if not resume_id:
                return Response(
                    {"errors": [{"detail": "Missing required relationship: resume"}]},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            try:
                resume = Resume.get(int(resume_id))
            except (TypeError, ValueError):
                resume = None

            if not resume:
                return Response(
                    {"errors": [{"detail": "Invalid resume ID"}]},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            content = attrs.get("content")
            if content:
                summary = Summary(
                    job_post_id=obj.id,
                    user_id=getattr(resume, "user_id", None),
                    content=content,
                )
                summary.save()
            else:
                summary_service = SummaryService(ai_client, job=obj, resume=resume)
                summary = summary_service.generate_summary()

            session = self.get_session()
            session.query(ResumeSummaries).filter_by(resume_id=resume.id).update(
                {ResumeSummaries.active: False}
            )
            session.add(
                ResumeSummaries(resume_id=resume.id, summary_id=summary.id, active=True)
            )
            session.commit()

            ser = SummarySerializer()
            payload = {"data": ser.to_resource(summary)}
            include_rels = self._parse_include(request)
            if include_rels:
                payload["included"] = self._build_included([summary], include_rels)
            return Response(payload, status=status.HTTP_201_CREATED)

        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [SummarySerializer().to_resource(s) for s in (obj.summaries or [])]
        return Response({"data": data})


class ScrapeViewSet(BaseSAViewSet):
    model = Scrape
    serializer_class = ScrapeSerializer

    def create(self, request):
        # Detect a "url" key in either a plain JSON body or JSON:API attributes
        data = request.data if isinstance(request.data, dict) else {}
        url = data.get("url")
        if url is None and isinstance(data.get("data"), dict):
            url = (data["data"].get("attributes") or {}).get("url")

        # Lazy import to avoid hard dependency at module import time
        from job_hunting.lib.browser_manager import BrowserManager

        # Lazy import to avoid heavy deps at module import time

        browser_manager = BrowserManager()
        asyncio.run(browser_manager.start_browser(False))
        service = GenericService(
            url=url, browser=browser_manager, ai_client=ai_client, creds={}
        )
        try:
            try:
                scrape = asyncio.run(service.process())
                asyncio.run(browser_manager.close_browser())
            except RuntimeError:
                # If an event loop is already running (e.g., under ASGI), use it
                loop = asyncio.get_event_loop()
                scrape = loop.run_until_complete(service.process())
                loop.run_until_complete(browser_manager.close_browser())
        except Exception as e:
            return Response(
                {"errors": [{"detail": f"Failed to process URL: {e}"}]},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # Try to resolve the created/linked JobPost; if not available, return 202 with the scrape
        session = self.get_session()
        job_post = None
        try:
            job_post = scrape.job_post
        except Exception:
            job_post = None

        if job_post:
            serializer = self.get_serializer()
            resource = serializer.to_resource(job_post)
            include_rels = self._parse_include(request)
            payload = {"data": resource}
            if include_rels:
                payload["included"] = self._build_included([job_post], include_rels)
            return Response(payload, status=status.HTTP_201_CREATED)

        # Could not resolve a JobPost yet — return the Scrape so the client can track progress
        ScrapeSerializer = TYPE_TO_SERIALIZER["scrape"]
        scr_ser = ScrapeSerializer()
        scrape_resource = scr_ser.to_resource(scrape)
        return Response({"data": scrape_resource}, status=status.HTTP_202_ACCEPTED)


class CompanyViewSet(BaseSAViewSet):
    model = Company
    serializer_class = CompanySerializer

    @action(detail=True, methods=["get"], url_path="job-posts")
    def job_posts(self, request, pk=None):
        company = self.model.get(int(pk))
        if not company:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [JobPostSerializer().to_resource(j) for j in (company.job_posts or [])]
        return Response({"data": data})

    @action(detail=True, methods=["get"])
    def scrapes(self, request, pk=None):
        company = self.model.get(int(pk))
        if not company:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [ScrapeSerializer().to_resource(s) for s in (company.scrapes or [])]
        return Response({"data": data})


class CoverLetterViewSet(BaseSAViewSet):
    model = CoverLetter
    serializer_class = CoverLetterSerializer

    def create(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        relationships = (data.get("data") or {}).get("relationships") or {}
        job_post_id = relationships.get("job-post", {}).get("data", {}).get("id")
        # Support both "resume" and "resumes" relationship keys
        resume_rel = relationships.get("resumes") or relationships.get("resume") or {}
        resume_id = resume_rel.get("data", {}).get("id")
        resume = Resume.get(resume_id)
        job_post = JobPost.get(job_post_id)
        cl_service = CoverLetterService(ai_client, job_post, resume)
        cover_letter = cl_service.generate_cover_letter()
        cover_letter.save()

        ser = self.get_serializer()
        payload = {"data": ser.to_resource(cover_letter)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([cover_letter], include_rels)
        return Response(payload, status=status.HTTP_201_CREATED)


class ApplicationViewSet(BaseSAViewSet):
    model = Application
    serializer_class = ApplicationSerializer


class ExperienceViewSet(BaseSAViewSet):
    model = Experience
    serializer_class = ExperienceSerializer

    @action(detail=True, methods=["get"])
    def descriptions(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = DescriptionSerializer()
        ser.set_parent_context("experience", obj.id, "descriptions")
        data = [ser.to_resource(d) for d in (obj.descriptions or [])]
        return Response({"data": data})

    def create(self, request):
        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response(
                {"errors": [{"detail": str(e)}]}, status=status.HTTP_400_BAD_REQUEST
            )

        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") or {}
        relationships = node.get("relationships") or {}

        # Accept both "resumes" (list) and "resume" (single)
        res_rel = relationships.get("resumes") or relationships.get("resume") or {}
        resume_ids = []
        if isinstance(res_rel, dict):
            d = res_rel.get("data")
            items = d if isinstance(d, list) else ([d] if isinstance(d, dict) else [])
            for it in items:
                if not isinstance(it, dict):
                    continue
                rid = it.get("id")
                if rid is None:
                    continue
                try:
                    resume_ids.append(int(rid))
                except (TypeError, ValueError):
                    return Response(
                        {"errors": [{"detail": f"Invalid resume id: {rid}"}]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

        # Validate referenced resumes (if provided)
        invalid = [rid for rid in resume_ids if Resume.get(rid) is None]
        if invalid:
            return Response(
                {
                    "errors": [
                        {
                            "detail": f"Invalid resume ID(s): {', '.join(map(str, invalid))}"
                        }
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        session = self.get_session()

        # Use existing Experience if client supplies an id; otherwise create a new one
        provided_id = node.get("id")
        exp = None
        if provided_id is not None:
            try:
                exp = Experience.get(int(provided_id))
            except (TypeError, ValueError):
                exp = None
            if not exp:
                return Response(
                    {"errors": [{"detail": f"Invalid experience ID: {provided_id}"}]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            exp = Experience(**attrs)
            session.add(exp)
            session.commit()  # ensure exp.id is available

        # Populate join table (avoid duplicates)
        for rid in resume_ids:
            ResumeExperience.first_or_create(resume_id=rid, experience_id=exp.id)
        session.commit()

        # Ensure relationships reflect the new joins
        session.expire(exp, ["resumes"])

        payload = {"data": ser.to_resource(exp)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([exp], include_rels)
        return Response(payload, status=status.HTTP_201_CREATED)

    def _upsert(self, request, pk, partial=False):
        session = self.get_session()
        exp = self.model.get(int(pk))
        if not exp:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response(
                {"errors": [{"detail": str(e)}]}, status=status.HTTP_400_BAD_REQUEST
            )

        # Update scalar attributes (including company via relationship_fks)
        for k, v in attrs.items():
            setattr(exp, k, v)
        session.add(exp)
        session.commit()

        # Parse resume relationship to add join(s)
        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") or {}
        relationships = node.get("relationships") or {}

        res_rel = relationships.get("resumes") or relationships.get("resume") or {}
        resume_ids = []
        if isinstance(res_rel, dict):
            d = res_rel.get("data")
            items = d if isinstance(d, list) else ([d] if isinstance(d, dict) else [])
            for it in items:
                if not isinstance(it, dict):
                    continue
                rid = it.get("id")
                if rid is None:
                    continue
                try:
                    resume_ids.append(int(rid))
                except (TypeError, ValueError):
                    return Response(
                        {"errors": [{"detail": f"Invalid resume id: {rid}"}]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

        # Validate resumes and create missing links
        invalid = [rid for rid in resume_ids if Resume.get(rid) is None]
        if invalid:
            return Response(
                {
                    "errors": [
                        {
                            "detail": f"Invalid resume ID(s): {', '.join(map(str, invalid))}"
                        }
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        for rid in resume_ids:
            ResumeExperience.first_or_create(
                session=session, resume_id=rid, experience_id=exp.id
            )
        session.commit()

        # Refresh relationships so response includes all linked resumes
        session.expire(exp, ["resumes"])

        payload = {"data": ser.to_resource(exp)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([exp], include_rels)
        return Response(payload)


class EducationViewSet(BaseSAViewSet):
    model = Education
    serializer_class = EducationSerializer

    def create(self, request):
        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response(
                {"errors": [{"detail": str(e)}]}, status=status.HTTP_400_BAD_REQUEST
            )

        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") or {}
        relationships = node.get("relationships") or {}

        # Accept both "resumes" (list) and "resume" (single) relationship keys
        res_rel = relationships.get("resumes") or relationships.get("resume") or {}
        resume_ids = []
        if isinstance(res_rel, dict):
            d = res_rel.get("data")
            items = d if isinstance(d, list) else ([d] if isinstance(d, dict) else [])
            for it in items:
                if not isinstance(it, dict):
                    continue
                rid = it.get("id")
                if rid is None:
                    continue
                try:
                    resume_ids.append(int(rid))
                except (TypeError, ValueError):
                    return Response(
                        {"errors": [{"detail": f"Invalid resume id: {rid}"}]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

        # Validate referenced resumes (if provided)
        invalid = [rid for rid in resume_ids if Resume.get(rid) is None]
        if invalid:
            return Response(
                {
                    "errors": [
                        {
                            "detail": f"Invalid resume ID(s): {', '.join(map(str, invalid))}"
                        }
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        session = self.get_session()

        # Create Education
        edu = Education(**attrs)
        session.add(edu)
        session.commit()  # ensure edu.id is available

        # Populate join table for each provided resume
        for rid in resume_ids:
            link = ResumeEducation(resume_id=rid, education_id=edu.id)
            session.add(link)
        session.commit()

        # Ensure relationships reflect the new joins
        session.expire(edu, ["resumes"])

        payload = {"data": ser.to_resource(edu)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([edu], include_rels)
        return Response(payload, status=status.HTTP_201_CREATED)


class CertificationViewSet(BaseSAViewSet):
    model = Certification
    serializer_class = CertificationSerializer

    def create(self, request):
        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response(
                {"errors": [{"detail": str(e)}]}, status=status.HTTP_400_BAD_REQUEST
            )

        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") or {}
        relationships = node.get("relationships") or {}

        # Accept both "resumes" (list) and "resume" (single)
        res_rel = relationships.get("resumes") or relationships.get("resume") or {}
        resume_ids = []
        if isinstance(res_rel, dict):
            d = res_rel.get("data")
            items = d if isinstance(d, list) else ([d] if isinstance(d, dict) else [])
            for it in items:
                if not isinstance(it, dict):
                    continue
                rid = it.get("id")
                if rid is None:
                    continue
                try:
                    resume_ids.append(int(rid))
                except (TypeError, ValueError):
                    return Response(
                        {"errors": [{"detail": f"Invalid resume id: {rid}"}]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

        # Validate referenced resumes (if provided)
        invalid = [rid for rid in resume_ids if Resume.get(rid) is None]
        if invalid:
            return Response(
                {
                    "errors": [
                        {
                            "detail": f"Invalid resume ID(s): {', '.join(map(str, invalid))}"
                        }
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        session = self.get_session()

        # Create Certification
        cert = Certification(**attrs)
        session.add(cert)
        session.commit()  # ensure cert.id is available

        # Populate join table
        for rid in resume_ids:
            link = ResumeCertification(resume_id=rid, certification_id=cert.id)
            session.add(link)
        session.commit()

        # Ensure relationships reflect the new joins
        session.expire(cert, ["resumes"])

        payload = {"data": ser.to_resource(cert)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([cert], include_rels)
        return Response(payload, status=status.HTTP_201_CREATED)


class DescriptionViewSet(BaseSAViewSet):
    model = Description
    serializer_class = DescriptionSerializer

    def create(self, request):
        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response(
                {"errors": [{"detail": str(e)}]}, status=status.HTTP_400_BAD_REQUEST
            )

        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") or {}
        relationships = node.get("relationships") or {}
        attrs_node = node.get("attributes") or {}
        global_order = attrs_node.get("order")

        exp_rel = (
            relationships.get("experiences") or relationships.get("experience") or {}
        )
        exp_items = []  # list of tuples (experience_id, order)
        if isinstance(exp_rel, dict):
            d = exp_rel.get("data")
            # Accept list or single object
            items = d if isinstance(d, list) else ([d] if isinstance(d, dict) else [])
            for it in items:
                if not isinstance(it, dict):
                    continue
                rid = it.get("id")
                if rid is None:
                    continue
                try:
                    eid = int(rid)
                except (TypeError, ValueError):
                    return Response(
                        {"errors": [{"detail": f"Invalid experience id: {rid}"}]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                order = None
                meta = it.get("meta")
                if isinstance(meta, dict) and "order" in meta:
                    try:
                        order = int(meta.get("order"))
                    except (TypeError, ValueError):
                        order = None
                if order is None and global_order is not None:
                    try:
                        order = int(global_order)
                    except (TypeError, ValueError):
                        order = None
                exp_items.append((eid, order))

        # Validate referenced experiences before creating description
        invalid_ids = [eid for eid, _ in exp_items if Experience.get(eid) is None]
        if invalid_ids:
            return Response(
                {
                    "errors": [
                        {
                            "detail": f"Invalid experience ID(s): {', '.join(map(str, invalid_ids))}"
                        }
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        session = self.get_session()

        # Create description
        desc = Description(**attrs)
        session.add(desc)
        session.commit()  # ensure desc.id is available

        # Populate join table with optional per-link order
        for eid, order in exp_items:
            link = ExperienceDescription(
                experience_id=eid,
                description_id=desc.id,
                order=(order if order is not None else 0),
            )
            session.add(link)
        session.commit()

        # Ensure relationships reflect the new joins
        session.expire(desc, ["experiences"])

        payload = {"data": ser.to_resource(desc)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([desc], include_rels)
        return Response(payload, status=status.HTTP_201_CREATED)

    def _upsert(self, request, pk, partial=False):
        session = self.get_session()
        desc = self.model.get(int(pk))
        if not desc:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)  # handles 'content'
        except ValueError as e:
            return Response(
                {"errors": [{"detail": str(e)}]}, status=status.HTTP_400_BAD_REQUEST
            )

        # Update scalar attributes
        for k, v in attrs.items():
            setattr(desc, k, v)
        session.add(desc)
        session.commit()

        # Handle experiences relationship and per-link 'order'
        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") or {}
        relationships = node.get("relationships") or {}
        attrs_node = node.get("attributes") or {}
        global_order = attrs_node.get("order")

        exp_rel = (
            relationships.get("experiences") or relationships.get("experience") or {}
        )
        if isinstance(exp_rel, dict):
            d = exp_rel.get("data")
            items = d if isinstance(d, list) else ([d] if isinstance(d, dict) else [])
            for it in items:
                if not isinstance(it, dict):
                    continue
                rid = it.get("id")
                if rid is None:
                    continue
                try:
                    eid = int(rid)
                except (TypeError, ValueError):
                    return Response(
                        {"errors": [{"detail": f"Invalid experience id: {rid}"}]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if Experience.get(eid) is None:
                    return Response(
                        {"errors": [{"detail": f"Invalid experience ID: {eid}"}]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                # Determine order: meta.order takes precedence; fallback to attributes.order
                order_val = None
                meta = it.get("meta")
                if isinstance(meta, dict) and "order" in meta:
                    try:
                        order_val = int(meta.get("order"))
                    except (TypeError, ValueError):
                        order_val = None
                if order_val is None and global_order is not None:
                    try:
                        order_val = int(global_order)
                    except (TypeError, ValueError):
                        order_val = None

                link = (
                    session.query(ExperienceDescription)
                    .filter_by(experience_id=eid, description_id=desc.id)
                    .first()
                )
                if not link:
                    session.add(
                        ExperienceDescription(
                            experience_id=eid,
                            description_id=desc.id,
                            order=(order_val if order_val is not None else 0),
                        )
                    )
                else:
                    if order_val is not None:
                        link.order = order_val
                    session.add(link)
            session.commit()

        try:
            session.expire(desc, ["experiences"])
        except Exception:
            pass

        payload = {"data": ser.to_resource(desc)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([desc], include_rels)
        return Response(payload)

    @action(detail=True, methods=["get"])
    def experiences(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [ExperienceSerializer().to_resource(e) for e in (obj.experiences or [])]
        return Response({"data": data})
