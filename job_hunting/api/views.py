import asyncio
import re
import dateparser
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth import get_user_model
from django.conf import settings
from rest_framework import viewsets, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.parsers import JSONParser, MultiPartParser
from .parsers import VndApiJSONParser
from job_hunting.lib.scoring.job_scorer import JobScorer
from job_hunting.lib.ai_client import get_client, set_api_key
from job_hunting.lib.services.summary_service import SummaryService
from job_hunting.lib.services.cover_letter_service import CoverLetterService
from job_hunting.lib.services.generic_service import GenericService
from job_hunting.lib.services.db_export_service import DbExportService
from job_hunting.lib.services.resume_export_service import ResumeExportService
from job_hunting.lib.services.ingest_resume import IngestResume

from job_hunting.lib.models import (
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
    Skill,
    ResumeSkill,
)
from .serializers import (
    DjangoUserSerializer,
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
    SkillSerializer,
    TYPE_TO_SERIALIZER,
    _parse_date,
)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def profile(request):
    """Get the current user's profile information."""
    ser = DjangoUserSerializer()
    resource = ser.to_resource(request.user)
    return Response({"data": resource})


@csrf_exempt
def healthcheck(request):
    # Compute bootstrap gate
    try:
        User = get_user_model()
        user_count = User.objects.count()
    except Exception:
        # If Django ORM isn't initialized yet, still report ok but unknown gate
        user_count = None

    if request.method == "GET":
        if user_count is None:
            status_str = "unknown"
            bootstrap_open = None
        else:
            bootstrap_open = user_count == 0
            status_str = "bootstrapped" if not bootstrap_open else "bootstrap"
        return JsonResponse(
            {
                "healthy": True,
                "status": status_str,
                "bootstrap_open": bootstrap_open,
            }
        )

    if request.method == "POST":
        # Allow setting the OpenAI API key after bootstrap as well.
        # Authorization options:
        # - Authenticated superuser may always set the key
        allow_bootstrap = getattr(settings, "ALLOW_BOOTSTRAP_SUPERUSER", False)
        bootstrap_token = getattr(settings, "BOOTSTRAP_TOKEN", "")
        user = getattr(request, "user", None)
        is_superuser = bool(
            user and user.is_authenticated and getattr(user, "is_superuser", False)
        )

        if not is_superuser:
            # Fallback to original bootstrap gate (no users yet and bootstrap enabled)
            if not allow_bootstrap or not bootstrap_token:
                return JsonResponse(
                    {"errors": [{"detail": "Bootstrap disabled"}]}, status=403
                )
            if user_count is not None and user_count > 0:
                return JsonResponse(
                    {"errors": [{"detail": "bootstrap closed"}]}, status=403
                )

        # Accept API key via header or JSON/JSON:API body
        api_key = (request.META.get("HTTP_X_OPENAI_API_KEY", "") or "").strip()
        if not api_key:
            try:
                import json

                data = json.loads(request.body.decode("utf-8") or "{}")
            except Exception:
                data = {}
            attrs = {}
            if isinstance(data.get("data"), dict):
                attrs = data["data"].get("attributes") or {}
            elif isinstance(data, dict):
                attrs = data
            api_key = (
                attrs.get("openai_api_key")
                or attrs.get("OPENAI_API_KEY")
                or attrs.get("openaiApiKey")
                or ""
            )
            api_key = str(api_key).strip()

        if not api_key:
            return JsonResponse(
                {"errors": [{"detail": "Missing openai_api_key"}]}, status=400
            )

        try:
            set_api_key(api_key)
        except Exception as e:
            return JsonResponse({"errors": [{"detail": str(e)}]}, status=400)

        return JsonResponse({"healthy": True, "openai_api_key_saved": True}, status=201)

    return JsonResponse({"error": "method not allowed"}, status=405)


class BaseSAViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, VndApiJSONParser, JSONParser]
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

    def _build_included(
        self, objs, include_rels, request=None, primary_serializer=None
    ):
        included = []
        seen = set()  # (type, id)
        primary_ser = primary_serializer or self.get_serializer()
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

                    # Filter cover-letter resources to only include those owned by authenticated user
                    if (
                        rel_type == "cover-letter"
                        and request
                        and hasattr(request, "user")
                        and request.user.is_authenticated
                    ):
                        if getattr(t, "user_id", None) != request.user.id:
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
            payload["included"] = self._build_included(items, include_rels, request)
        return Response(payload)

    def retrieve(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer()
        payload = {"data": ser.to_resource(obj)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([obj], include_rels, request)
        return Response(payload)

    def create(self, request):
        # Handle both JSON:API and plain JSON payloads
        data = request.data if isinstance(request.data, dict) else {}

        # Check if this is JSON:API format (has "data" wrapper)
        if "data" in data:
            # Use JSON:API parser
            ser = self.get_serializer()
            try:
                attrs = ser.parse_payload(request.data)
            except ValueError as e:
                return Response({"errors": [{"detail": str(e)}]}, status=400)
        else:
            # Handle plain JSON format
            attrs = {}
            for key in [
                "username",
                "email",
                "first_name",
                "last_name",
                "password",
                "phone",
            ]:
                if key in data:
                    attrs[key] = data[key]
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

            client = get_client(required=False)
            if client is None:
                return Response(
                    {
                        "errors": [
                            {"detail": "AI client not configured. Set OPENAI_API_KEY."}
                        ]
                    },
                    status=503,
                )

            summary_service = SummaryService(client, job=job_post, resume=resume)
            summary = summary_service.generate_summary()

        session = self.get_session()
        # Deactivate existing links, then create new active link
        session.query(ResumeSummaries).filter_by(resume_id=resume.id).update(
            {ResumeSummaries.active: False}, synchronize_session=False
        )
        session.add(
            ResumeSummaries(resume_id=resume.id, summary_id=summary.id, active=True)
        )
        session.commit()
        ResumeSummaries.ensure_single_active_for_resume(resume.id, session=session)
        session.commit()

        ser = self.get_serializer()
        payload = {"data": ser.to_resource(summary)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([summary], include_rels, request)
        return Response(payload, status=status.HTTP_201_CREATED)


class DjangoUserViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]
    parser_classes = [VndApiJSONParser, JSONParser]

    def get_permissions(self):
        """Allow unauthenticated access for create and bootstrap_superuser actions."""
        if self.action in ["create", "bootstrap_superuser"]:
            return [AllowAny()]
        return super().get_permissions()

    def get_serializer(self):
        return DjangoUserSerializer()

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

        for obj in objs:
            for rel in include_rels:
                rel_type, targets = primary_ser.get_related(obj, rel)
                if not rel_type:
                    continue
                ser_cls = TYPE_TO_SERIALIZER.get(rel_type)
                if not ser_cls:
                    continue
                rel_ser = ser_cls()
                for t in targets:
                    key = (rel_type, str(t.id))
                    if key in seen:
                        continue
                    seen.add(key)
                    included.append(rel_ser.to_resource(t))
        return included

    def list(self, request):
        User = get_user_model()

        # Restrict list to staff users or return only current user
        if request.user.is_staff:
            users = User.objects.all()
        else:
            users = [request.user]

        ser = self.get_serializer()
        data = [ser.to_resource(u) for u in users]
        payload = {"data": data}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included(users, include_rels, request)
        return Response(payload)

    def retrieve(self, request, pk=None):
        User = get_user_model()
        try:
            user = User.objects.get(id=int(pk))
        except (User.DoesNotExist, ValueError):
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        # Only allow staff to retrieve other users
        if not request.user.is_staff and user.id != request.user.id:
            return Response({"errors": [{"detail": "Forbidden"}]}, status=403)

        ser = self.get_serializer()
        payload = {"data": ser.to_resource(user)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([user], include_rels, request)
        return Response(payload)

    @action(
        detail=False,
        methods=["post"],
        url_path="set-openai-api-key",
        permission_classes=[IsAuthenticated],
    )
    def set_openai_api_key(self, request):
        user = request.user
        if not getattr(user, "is_superuser", False):
            return Response({"errors": [{"detail": "Forbidden"}]}, status=403)

        # Accept API key via header or JSON/JSON:API body
        api_key = (request.META.get("HTTP_X_OPENAI_API_KEY", "") or "").strip()
        if not api_key:
            data = request.data if isinstance(request.data, dict) else {}
            attrs = {}
            if isinstance(data.get("data"), dict):
                attrs = data["data"].get("attributes") or {}
            else:
                attrs = data or {}
            api_key = (
                attrs.get("openai_api_key")
                or attrs.get("OPENAI_API_KEY")
                or attrs.get("openaiApiKey")
                or ""
            )
            api_key = str(api_key).strip()

        if not api_key:
            return Response(
                {"errors": [{"detail": "Missing openai_api_key"}]}, status=400
            )

        try:
            set_api_key(api_key)
        except Exception as e:
            return Response({"errors": [{"detail": str(e)}]}, status=400)

        return Response({"meta": {"openai_api_key_saved": True}}, status=201)

    def create(self, request):
        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response({"errors": [{"detail": str(e)}]}, status=400)

        # Extract phone before user creation
        phone_present = "phone" in attrs
        phone_val = str(attrs.pop("phone", "") or "").strip() if phone_present else None

        username = attrs.get("username")
        password = attrs.get("password")
        email = attrs.get("email", "")
        first_name = attrs.get("first_name", "")
        last_name = attrs.get("last_name", "")

        if not username:
            return Response(
                {"errors": [{"detail": "Username is required"}]}, status=400
            )
        if not password:
            return Response(
                {"errors": [{"detail": "Password is required"}]}, status=400
            )

        User = get_user_model()

        # Check uniqueness
        if User.objects.filter(username=username).exists():
            return Response(
                {"errors": [{"detail": "Username already exists"}]}, status=400
            )
        if email and User.objects.filter(email=email).exists():
            return Response(
                {"errors": [{"detail": "Email already exists"}]}, status=400
            )

        user = User(
            username=username, email=email, first_name=first_name, last_name=last_name
        )
        user.set_password(password)
        user.save()

        # Handle phone via SQLAlchemy Profile if provided
        if phone_present:
            from job_hunting.lib.models import Profile

            session = Profile.get_session()
            prof = session.query(Profile).filter_by(user_id=user.id).first()
            if not prof:
                prof = Profile(
                    user_id=user.id,
                    phone=(phone_val[:50] or None) if phone_val else None,
                )
            else:
                prof.phone = (phone_val[:50] or None) if phone_val else None
            session.add(prof)
            session.commit()

        return Response({"data": ser.to_resource(user)}, status=status.HTTP_201_CREATED)

    def update(self, request, pk=None):
        return self._upsert(request, pk, partial=False)

    def partial_update(self, request, pk=None):
        return self._upsert(request, pk, partial=True)

    def _upsert(self, request, pk, partial=False):
        User = get_user_model()
        try:
            user = User.objects.get(id=int(pk))
        except (User.DoesNotExist, ValueError):
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response({"errors": [{"detail": str(e)}]}, status=400)

        # Extract phone before user updates
        phone_present = "phone" in attrs
        phone_val = str(attrs.pop("phone", "") or "").strip() if phone_present else None

        # Update allowed fields
        if "email" in attrs:
            user.email = attrs["email"]
        if "first_name" in attrs:
            user.first_name = attrs["first_name"]
        if "last_name" in attrs:
            user.last_name = attrs["last_name"]
        if "password" in attrs:
            user.set_password(attrs["password"])

        user.save()

        # Handle phone via SQLAlchemy Profile if provided
        if phone_present:
            from job_hunting.lib.models.profile import Profile

            session = Profile.get_session()
            prof = session.query(Profile).filter_by(user_id=user.id).first()
            if not prof:
                prof = Profile(
                    user_id=user.id,
                    phone=(phone_val[:50] or None) if phone_val else None,
                )
            else:
                prof.phone = (phone_val[:50] or None) if phone_val else None
            session.add(prof)
            session.commit()

        return Response({"data": ser.to_resource(user)})

    def destroy(self, request, pk=None):
        User = get_user_model()
        try:
            user = User.objects.get(id=int(pk))
            user.delete()
        except (User.DoesNotExist, ValueError):
            pass
        return Response(status=204)

    @action(
        detail=False,
        methods=["post"],
        url_path="bootstrap-superuser",
        permission_classes=[AllowAny],
    )
    def bootstrap_superuser(self, request):

        # Only allow when no users exist
        User = get_user_model()
        if User.objects.count() > 0:
            return Response({"errors": [{"detail": "bootstrap closed"}]}, status=403)

        # Accept both JSON:API and plain JSON
        data = request.data if isinstance(request.data, dict) else {}
        attrs = {}
        if isinstance(data.get("data"), dict):
            attrs = data["data"].get("attributes") or {}
        else:
            attrs = data or {}

        username = attrs.get("username") or attrs.get("name") or "admin"
        email = (attrs.get("email") or None) or None
        password = attrs.get("password") or "admin"
        first_name = attrs.get("first_name") or attrs.get("name") or ""
        last_name = attrs.get("last_name") or ""
        # Optional: allow setting OpenAI API key during bootstrap
        api_key = (
            attrs.get("openai_api_key")
            or attrs.get("OPENAI_API_KEY")
            or attrs.get("openaiApiKey")
        )

        user = User.objects.create_superuser(
            username=username,
            email=email,
            password=password,
        )
        if first_name:
            user.first_name = first_name
        if last_name:
            user.last_name = last_name
        user.save()

        # Optionally persist the AI API key securely during bootstrap
        meta = {}
        if api_key:
            try:
                set_api_key(str(api_key).strip())
                meta["openai_api_key_saved"] = True
            except Exception as e:
                meta["openai_api_key_saved"] = False
                meta["openai_api_key_error"] = str(e)

        ser = self.get_serializer()
        payload = {"data": ser.to_resource(user)}
        if meta:
            payload["meta"] = meta
        return Response(payload, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def resumes(self, request, pk=None):
        User = get_user_model()
        try:
            user = User.objects.get(id=int(pk))
        except (User.DoesNotExist, ValueError):
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        session = Resume.get_session()
        resumes = session.query(Resume).filter_by(user_id=user.id).all()
        data = [ResumeSerializer().to_resource(r) for r in resumes]
        return Response({"data": data})

    @action(detail=True, methods=["get"])
    def scores(self, request, pk=None):
        User = get_user_model()
        try:
            user = User.objects.get(id=int(pk))
        except (User.DoesNotExist, ValueError):
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        session = Score.get_session()
        scores = session.query(Score).filter_by(user_id=user.id).all()
        data = [ScoreSerializer().to_resource(s) for s in scores]
        return Response({"data": data})

    @action(detail=True, methods=["get"], url_path="cover-letters")
    def cover_letters(self, request, pk=None):
        User = get_user_model()
        try:
            user = User.objects.get(id=int(pk))
        except (User.DoesNotExist, ValueError):
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        session = CoverLetter.get_session()
        cover_letters = session.query(CoverLetter).filter_by(user_id=user.id).all()
        data = [CoverLetterSerializer().to_resource(c) for c in cover_letters]
        return Response({"data": data})

    @action(detail=True, methods=["get"])
    def applications(self, request, pk=None):
        User = get_user_model()
        try:
            user = User.objects.get(id=int(pk))
        except (User.DoesNotExist, ValueError):
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        session = Application.get_session()
        applications = session.query(Application).filter_by(user_id=user.id).all()
        data = [ApplicationSerializer().to_resource(a) for a in applications]
        return Response({"data": data})

    @action(detail=True, methods=["get"])
    def summaries(self, request, pk=None):
        User = get_user_model()
        try:
            user = User.objects.get(id=int(pk))
        except (User.DoesNotExist, ValueError):
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        session = Summary.get_session()
        summaries = session.query(Summary).filter_by(user_id=user.id).all()
        data = [SummarySerializer().to_resource(s) for s in summaries]
        return Response({"data": data})

    @action(
        detail=False,
        methods=["get"],
        url_path="me",
        permission_classes=[IsAuthenticated],
    )
    def me(self, request):
        user = request.user
        ser = self.get_serializer()
        payload = {"data": ser.to_resource(user)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([user], include_rels, request)
        return Response(payload)


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
            payload["included"] = self._build_included(items, include_rels, request)
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
            payload["included"] = self._build_included([obj], include_rels, request)
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

        # Update scalar attributes
        for k, v in attrs.items():
            setattr(obj, k, v)
        session.add(obj)
        session.commit()

        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") or {}
        attrs_node = node.get("attributes") or {}

        # Optional: update active summary content if attributes.summary is provided
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
                ).update({ResumeSummaries.active: False}, synchronize_session=False)
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
                    {ResumeSummaries.active: False}, synchronize_session=False
                )
                session.add(
                    ResumeSummaries(resume_id=obj.id, summary_id=sm.id, active=True)
                )
                session.commit()

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
                exp = Experience.get(exp_id)
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

                session.add(exp)
                session.flush()  # ensure exp.id

                # Ensure resume <-> experience link exists
                ResumeExperience.first_or_create(
                    session=session, resume_id=obj.id, experience_id=exp.id
                )

                # Reconcile descriptions for this experience (keep order as provided)
                desc_nodes = (exp_rels.get("descriptions") or {}).get("data") or []
                desired_desc_ids_ordered = []
                invalid_desc_ids = []
                for d in desc_nodes:
                    did = _int_or_none((d or {}).get("id"))
                    if did is None:
                        continue
                    if Description.get(did) is None:
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
                    existing_links = (
                        session.query(ExperienceDescription)
                        .filter_by(experience_id=exp.id)
                        .all()
                    )
                    existing_by_desc = {l.description_id: l for l in existing_links}
                    desired_set = set(desired_desc_ids_ordered)
                    existing_set = set(existing_by_desc.keys())

                    # Remove links not desired
                    to_remove = existing_set - desired_set
                    if to_remove:
                        session.query(ExperienceDescription).filter(
                            ExperienceDescription.experience_id == exp.id,
                            ExperienceDescription.description_id.in_(list(to_remove)),
                        ).delete(synchronize_session=False)

                    # Add/update desired links with order
                    for order_idx, did in enumerate(desired_desc_ids_ordered):
                        link = existing_by_desc.get(did)
                        if not link:
                            session.add(
                                ExperienceDescription(
                                    experience_id=exp.id,
                                    description_id=did,
                                    order=order_idx,
                                )
                            )
                        else:
                            link.order = order_idx
                            session.add(link)

                desired_exp_ids.append(exp.id)

            # Reconcile resume_experience set to match provided experiences
            existing_links = (
                session.query(ResumeExperience).filter_by(resume_id=obj.id).all()
            )
            existing_ids = {l.experience_id for l in existing_links}
            desired_ids = set(desired_exp_ids)
            to_add = desired_ids - existing_ids
            to_remove = existing_ids - desired_ids

            for eid in to_add:
                ResumeExperience.first_or_create(
                    session=session, resume_id=obj.id, experience_id=eid
                )
            if to_remove:
                session.query(ResumeExperience).filter(
                    ResumeExperience.resume_id == obj.id,
                    ResumeExperience.experience_id.in_(list(to_remove)),
                ).delete(synchronize_session=False)

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
                if Education.get(eid) is None:
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
            existing_links = (
                session.query(ResumeEducation).filter_by(resume_id=obj.id).all()
            )
            existing_ids = {l.education_id for l in existing_links}
            to_add = desired_ids - existing_ids
            to_remove = existing_ids - desired_ids
            for eid in to_add:
                session.add(ResumeEducation(resume_id=obj.id, education_id=eid))
            if to_remove:
                session.query(ResumeEducation).filter(
                    ResumeEducation.resume_id == obj.id,
                    ResumeEducation.education_id.in_(list(to_remove)),
                ).delete(synchronize_session=False)

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
                if Certification.get(cid) is None:
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
            existing_links = (
                session.query(ResumeCertification).filter_by(resume_id=obj.id).all()
            )
            existing_ids = {l.certification_id for l in existing_links}
            to_add = desired_ids - existing_ids
            to_remove = existing_ids - desired_ids
            for cid in to_add:
                session.add(ResumeCertification(resume_id=obj.id, certification_id=cid))
            if to_remove:
                session.query(ResumeCertification).filter(
                    ResumeCertification.resume_id == obj.id,
                    ResumeCertification.certification_id.in_(list(to_remove)),
                ).delete(synchronize_session=False)

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
                    skill = Skill.get(sid)
                    if not skill:
                        invalid.append(sid)
                        continue
                else:
                    s_attrs = s_node.get("attributes") or {}
                    text = s_attrs.get("text") or s_node.get("text")
                    if not text:
                        continue
                    # Create or find by text
                    skill, _ = Skill.first_or_create(
                        session=session, text=str(text).strip()
                    )

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

            existing_links = (
                session.query(ResumeSkill).filter_by(resume_id=obj.id).all()
            )
            existing_ids = {l.skill_id for l in existing_links}
            desired_ids = set(desired_active_by_id.keys())

            to_add = desired_ids - existing_ids
            to_remove = existing_ids - desired_ids
            to_update = desired_ids & existing_ids

            # Remove undesired links
            if to_remove:
                session.query(ResumeSkill).filter(
                    ResumeSkill.resume_id == obj.id,
                    ResumeSkill.skill_id.in_(list(to_remove)),
                ).delete(synchronize_session=False)

            # Add missing links
            for sid in to_add:
                ResumeSkill.first_or_create(
                    session=session,
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
                        session.add(link)

            session.commit()

        # Summaries: reconcile join set if provided (preserve current active if still present)
        summaries_in = node.get("summaries") or data.get("summaries")
        if summaries_in is not None:
            desired_ids = set()
            invalid = []
            desired_active_sid = None
            for item in summaries_in or []:
                s_node = (item or {}).get("data") or item or {}
                sid = _int_or_none(s_node.get("id"))
                if sid is None:
                    continue
                if Summary.get(sid) is None:
                    invalid.append(sid)
                else:
                    desired_ids.add(sid)
                    # Check for explicit active flag
                    active_flag = (s_node.get("attributes") or {}).get("active")
                    if active_flag is not None and active_flag:
                        desired_active_sid = sid
            if invalid:
                return Response(
                    {
                        "errors": [
                            {
                                "detail": f"Invalid summary ID(s): {', '.join(map(str, invalid))}"
                            }
                        ]
                    },
                    status=400,
                )
            existing_links = (
                session.query(ResumeSummaries).filter_by(resume_id=obj.id).all()
            )
            existing_ids = {l.summary_id for l in existing_links}
            active_by_id = {l.summary_id: l.active for l in existing_links}

            to_add = desired_ids - existing_ids
            to_remove = existing_ids - desired_ids

            for sid in to_add:
                session.add(
                    ResumeSummaries(resume_id=obj.id, summary_id=sid, active=False)
                )
            if to_remove:
                session.query(ResumeSummaries).filter(
                    ResumeSummaries.resume_id == obj.id,
                    ResumeSummaries.summary_id.in_(list(to_remove)),
                ).delete(synchronize_session=False)

            # Update active flag if explicitly requested, otherwise preserve existing active
            session.flush()
            if desired_active_sid is not None:
                # Set requested summary as active and deactivate all others
                session.query(ResumeSummaries).filter(
                    ResumeSummaries.resume_id == obj.id
                ).update({ResumeSummaries.active: False}, synchronize_session=False)
                session.query(ResumeSummaries).filter_by(
                    resume_id=obj.id, summary_id=desired_active_sid
                ).update({ResumeSummaries.active: True}, synchronize_session=False)
            else:
                # Preserve active on the one that remains, otherwise none active
                still_existing = desired_ids
                if still_existing:
                    # If there is exactly one active that remains, re-assert it and clear others
                    active_remaining = [
                        sid for sid in still_existing if active_by_id.get(sid)
                    ]
                    if active_remaining:
                        keep_sid = active_remaining[0]
                        session.query(ResumeSummaries).filter(
                            ResumeSummaries.resume_id == obj.id
                        ).update(
                            {ResumeSummaries.active: False}, synchronize_session=False
                        )
                        session.query(ResumeSummaries).filter_by(
                            resume_id=obj.id, summary_id=keep_sid
                        ).update(
                            {ResumeSummaries.active: True}, synchronize_session=False
                        )

        # Final commit and refresh relationships for response
        session.commit()
        # Enforce exactly one active summary if any exist
        ResumeSummaries.ensure_single_active_for_resume(obj.id, session=session)
        session.commit()
        try:
            session.expire(
                obj,
                ["experiences", "educations", "certifications", "summaries", "skills"],
            )
        except Exception:
            pass

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
                skill = Skill.get(sid)
            if skill is None:
                s_attrs = s_node.get("attributes") or {}
                text = s_attrs.get("text") or s_node.get("text")
                if not text:
                    continue  # ignore invalid entries
                skill, _ = Skill.first_or_create(
                    session=session, text=str(text).strip()
                )
            # Determine 'active' (default True)
            active_val = (s_node.get("attributes") or {}).get("active")
            active_val = bool(active_val) if active_val is not None else True
            ResumeSkill.first_or_create(
                session=session,
                resume_id=resume.id,
                skill_id=skill.id,
                defaults={"active": active_val},
            )
        session.commit()

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
                    summary = Summary.get(int(sid))
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

                summary = Summary(job_post_id=jp_id, user_id=u_id, content=content)
                summary.save()

            # Link summary to resume; mark the first one as active
            session.add(
                ResumeSummaries(
                    resume_id=resume.id,
                    summary_id=summary.id,
                    active=(not active_set),
                )
            )
            active_set = True
        session.commit()
        ResumeSummaries.ensure_single_active_for_resume(resume.id, session=session)
        session.commit()

        # Refresh relationships so response includes all links
        try:
            session.expire(
                resume,
                ["experiences", "educations", "certifications", "summaries", "skills"],
            )
        except Exception:
            pass

        payload = {"data": ser.to_resource(resume)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([resume], include_rels, request)
        return Response(payload, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def scores(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [ScoreSerializer().to_resource(s) for s in (obj.scores or [])]
        return Response({"data": data})

    @action(
        detail=True,
        methods=["get"],
        url_path="cover-letters",
        permission_classes=[IsAuthenticated],
    )
    def cover_letters(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        session = CoverLetter.get_session()
        cover_letters = (
            session.query(CoverLetter)
            .filter_by(resume_id=obj.id, user_id=request.user.id)
            .all()
        )
        data = [CoverLetterSerializer().to_resource(c) for c in cover_letters]
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

            session = self.get_session()
            session.query(ResumeSummaries).filter_by(resume_id=obj.id).update(
                {ResumeSummaries.active: False}, synchronize_session=False
            )
            session.add(
                ResumeSummaries(resume_id=obj.id, summary_id=summary.id, active=True)
            )
            session.commit()
            ResumeSummaries.ensure_single_active_for_resume(obj.id, session=session)
            session.commit()

            ser = SummarySerializer()
            payload = {"data": ser.to_resource(summary)}
            include_rels = self._parse_include(request)
            if include_rels:
                payload["included"] = self._build_included(
                    [summary], include_rels, request
                )
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
            payload["included"] = self._build_included([summary], include_rels, request)
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

    @action(detail=True, methods=["get"])
    def skills(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = SkillSerializer()
        ser.set_parent_context("resume", obj.id, "skills")
        items = list(obj.skills or [])
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

            for skill in items:
                for rel in include_rels:
                    rel = _normalize_rel(rel)
                    rel_type, targets = ser.get_related(skill, rel)
                    if not rel_type:
                        continue
                    ser_cls = TYPE_TO_SERIALIZER.get(rel_type)
                    if not ser_cls:
                        continue
                    rel_ser = ser_cls()
                    if hasattr(rel_ser, "set_parent_context"):
                        rel_ser.set_parent_context(ser.type, skill.id, rel)
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

    @action(detail=True, methods=["get"], url_path="export")
    def export(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        format_param = request.query_params.get("format", "docx").lower()
        template_path_param = request.query_params.get("template_path")

        if format_param == "md":
            # Export as markdown
            exporter = DbExportService()
            markdown_content = exporter.resume_markdown_export(obj)

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
            filename = "-".join([p for p in filename_parts if p]) + ".md"

            response = HttpResponse(
                markdown_content.encode("utf-8"),
                content_type="text/markdown; charset=utf-8",
            )
            response["Content-Disposition"] = f'attachment; filename="{filename}"'
            return response

        else:
            # Export as DOCX (default)
            try:
                svc = ResumeExportService()
                data = svc.render_docx(obj, template_path=template_path_param)
            except ImportError:
                return Response(
                    {
                        "errors": [
                            {"detail": "DOCX export requires 'docxtpl' to be installed"}
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

    @action(detail=False, methods=["post"], url_path="ingest")
    def ingest(self, request):
        """
        Ingest a resume from an uploaded docx file and create a new resume.

        Expects a multipart/form-data request with a 'file' field containing the docx.
        """
        # Check if file was uploaded
        if "file" not in request.FILES:
            return Response(
                {
                    "errors": [
                        {
                            "detail": "No file uploaded. Expected 'file' field with docx content."
                        }
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        uploaded_file = request.FILES["file"]

        # Validate file type
        if not uploaded_file.name.lower().endswith(".docx"):
            return Response(
                {"errors": [{"detail": "Only .docx files are supported"}]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            # Read file content as blob
            file_blob = uploaded_file.read()

            # Create a new resume for the authenticated user
            # resume = Resume(user_id=request.user.id, file_path=uploaded_file.name)

            # Create IngestResume service with the blob
            ingest_service = IngestResume(
                user=request.user,
                resume=file_blob,  # Pass blob instead of path
                agent=None,  # Will use default agent
            )

            # Process the resume
            result = ingest_service.process()
            resume = result or ingest_service.db_resume

            # Guard against None resume from failed ingestion
            if resume is None:
                return Response(
                    {"errors": [{"detail": "Ingest failed: no resume created"}]},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Return the created resume with all relationships
            ser = self.get_serializer()
            payload = {"data": ser.to_resource(resume)}

            # Include all relationships by default for ingest response
            ser_rels = list(getattr(ser, "relationships", {}).keys())
            include_rels = self._parse_include(request) or ser_rels
            if include_rels:
                payload["included"] = self._build_included(
                    [resume], include_rels, request
                )

            return Response(payload, status=status.HTTP_201_CREATED)

        except Exception as e:
            return Response(
                {"errors": [{"detail": f"Failed to process resume: {str(e)}"}]},
                status=status.HTTP_400_BAD_REQUEST,
            )


class ScoreViewSet(BaseSAViewSet):
    model = Score
    serializer_class = ScoreSerializer

    def create(self, request):
        data = request.data if isinstance(request.data, dict) else {}

        client = get_client(required=False)
        if client is None:
            return Response(
                {
                    "errors": [
                        {"detail": "AI client not configured. Set OPENAI_API_KEY."}
                    ]
                },
                status=503,
            )

        myJobScorer = JobScorer(client)

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
            payload["included"] = self._build_included([myScore], include_rels, request)
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

    @action(
        detail=True,
        methods=["get"],
        url_path="cover-letters",
        permission_classes=[IsAuthenticated],
    )
    def cover_letters(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        session = CoverLetter.get_session()
        cover_letters = (
            session.query(CoverLetter)
            .filter_by(job_post_id=obj.id, user_id=request.user.id)
            .all()
        )
        data = [CoverLetterSerializer().to_resource(c) for c in cover_letters]
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

                summary_service = SummaryService(client, job=obj, resume=resume)
                summary = summary_service.generate_summary()

            session = self.get_session()
            session.query(ResumeSummaries).filter_by(resume_id=resume.id).update(
                {ResumeSummaries.active: False}, synchronize_session=False
            )
            session.add(
                ResumeSummaries(resume_id=resume.id, summary_id=summary.id, active=True)
            )
            session.commit()
            ResumeSummaries.ensure_single_active_for_resume(resume.id, session=session)
            session.commit()

            ser = SummarySerializer()
            payload = {"data": ser.to_resource(summary)}
            include_rels = self._parse_include(request)
            if include_rels:
                payload["included"] = self._build_included(
                    [summary], include_rels, request
                )
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

        # Check if scraping is enabled
        if not getattr(settings, "SCRAPING_ENABLED", False):
            return Response(
                {"errors": [{"detail": "Scraping functionality is disabled"}]},
                status=status.HTTP_501_NOT_IMPLEMENTED,
            )

        # Detect a "url" key in either a plain JSON body or JSON:API attributes
        data = request.data if isinstance(request.data, dict) else {}
        url = data.get("url")
        if url is None and isinstance(data.get("data"), dict):
            url = (data["data"].get("attributes") or {}).get("url")

        # Lazy import to avoid hard dependency at module import time
        from job_hunting.lib.browser_manager import BrowserManager

        # Lazy import to avoid heavy deps at module import time

        client = get_client(required=False)
        if client is None:
            return Response(
                {
                    "errors": [
                        {"detail": "AI client not configured. Set OPENAI_API_KEY."}
                    ]
                },
                status=503,
            )

        browser_manager = BrowserManager()
        asyncio.run(browser_manager.start_browser(False))
        service = GenericService(
            url=url, browser=browser_manager, ai_client=client, creds={}
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
            job_post_serializer = TYPE_TO_SERIALIZER.get("job-post")()
            resource = job_post_serializer.to_resource(job_post)
            include_rels = self._parse_include(request)
            payload = {"data": resource}
            if include_rels:
                payload["included"] = self._build_included(
                    [job_post],
                    include_rels,
                    request,
                    primary_serializer=job_post_serializer,
                )
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
    permission_classes = [IsAuthenticated]

    def list(self, request):
        session = self.get_session()
        items = session.query(self.model).filter_by(user_id=request.user.id).all()
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

        # Verify ownership
        if obj.user_id != request.user.id:
            return Response(
                {"errors": [{"detail": "Forbidden"}]},
                status=status.HTTP_403_FORBIDDEN,
            )

        ser = self.get_serializer()
        payload = {"data": ser.to_resource(obj)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([obj], include_rels, request)
        return Response(payload)

    def _upsert(self, request, pk, partial=False):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        # Verify ownership
        if obj.user_id != request.user.id:
            return Response(
                {"errors": [{"detail": "Forbidden"}]},
                status=status.HTTP_403_FORBIDDEN,
            )

        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response({"errors": [{"detail": str(e)}]}, status=400)

        # Remove any user_id from attrs to prevent ownership changes
        attrs.pop("user_id", None)

        # If resume_id is being changed, validate new resume ownership
        if "resume_id" in attrs:
            new_resume = Resume.get(attrs["resume_id"])
            if not new_resume or new_resume.user_id != request.user.id:
                return Response(
                    {"errors": [{"detail": "Forbidden"}]},
                    status=status.HTTP_403_FORBIDDEN,
                )

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

        # Verify ownership
        if obj.user_id != request.user.id:
            return Response(
                {"errors": [{"detail": "Forbidden"}]},
                status=status.HTTP_403_FORBIDDEN,
            )

        session = self.get_session()
        session.delete(obj)
        session.commit()
        return Response(status=204)

    def create(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(data)
        except ValueError as e:
            return Response(
                {"errors": [{"detail": str(e)}]}, status=status.HTTP_400_BAD_REQUEST
            )

        node = data.get("data") or {}
        relationships = node.get("relationships") or {}
        attrs_node = node.get("attributes") or {}

        def _first_id(n):
            if isinstance(n, dict):
                d = n.get("data", n)
                if isinstance(d, dict) and "id" in d:
                    return d.get("id")
            return None

        def _rel_id(*keys):
            for k in keys:
                rel = relationships.get(k)
                if rel is not None:
                    rid = _first_id(rel)
                    if rid is not None:
                        return rid
            return None

        # Resolve IDs from attrs (serializer) or relationships fallbacks
        resume_id = attrs.get("resume_id") or _rel_id("resume", "resumes")
        job_post_id = attrs.get("job_post_id") or _rel_id(
            "job-post", "job_post", "jobPost", "job-posts", "jobPosts"
        )

        try:
            resume_id = int(resume_id) if resume_id is not None else None
            job_post_id = int(job_post_id) if job_post_id is not None else None
        except (TypeError, ValueError):
            return Response(
                {"errors": [{"detail": "Invalid resume or job-post ID"}]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if resume_id is None or job_post_id is None:
            return Response(
                {
                    "errors": [
                        {
                            "detail": "Missing required relationships: resume and job-post"
                        }
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        resume = Resume.get(resume_id)
        job_post = JobPost.get(job_post_id)
        if not resume or not job_post:
            return Response(
                {"errors": [{"detail": "Invalid resume or job-post ID"}]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate that the referenced resume belongs to the authenticated user
        if resume.user_id != request.user.id:
            return Response(
                {
                    "errors": [
                        {
                            "detail": "Forbidden: resume does not belong to the authenticated user"
                        }
                    ]
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        # Force ownership to authenticated user
        user_id = request.user.id

        # Accept provided content; otherwise, generate via AI
        content = attrs_node.get("content")
        if content:
            cover_letter = CoverLetter(
                content=content,
                user_id=user_id,
                resume_id=resume.id,
                job_post_id=job_post.id,
            )
            cover_letter.save()
        else:
            client = get_client(required=False)
            if client is None:
                return Response(
                    {
                        "errors": [
                            {"detail": "AI client not configured. Set OPENAI_API_KEY."}
                        ]
                    },
                    status=503,
                )

            cl_service = CoverLetterService(client, job_post, resume)
            cover_letter = cl_service.generate_cover_letter()

        payload = {"data": ser.to_resource(cover_letter)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included(
                [cover_letter], include_rels, request
            )
        return Response(payload, status=status.HTTP_201_CREATED)

    @action(
        detail=True,
        methods=["get"],
        url_path="export",
        permission_classes=[IsAuthenticated],
    )
    def export_docx(self, request, pk=None):
        cl = self.model.get(int(pk))
        if not cl:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        # Verify ownership
        if cl.user_id != request.user.id:
            return Response(
                {"errors": [{"detail": "Forbidden"}]},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            from docx import Document  # python-docx
        except Exception:
            return Response(
                {
                    "errors": [
                        {"detail": "DOCX export requires 'python-docx' to be installed"}
                    ]
                },
                status=status.HTTP_501_NOT_IMPLEMENTED,
            )

        from io import BytesIO

        doc = Document()

        # Title
        title_parts = ["Cover Letter"]
        try:
            if getattr(cl, "job_post", None) and getattr(cl.job_post, "title", None):
                title_parts.append(str(cl.job_post.title))
            if getattr(cl, "job_post", None) and getattr(cl.job_post, "company", None):
                if getattr(cl.job_post.company, "name", None):
                    title_parts.append(str(cl.job_post.company.name))
        except Exception:
            pass
        if title_parts:
            doc.add_heading(" - ".join(title_parts), level=1)

        # Body
        content = (cl.content or "").strip()
        if content:
            import re as _re

            for para in [p for p in _re.split(r"\n\s*\n", content) if p.strip()]:
                p = doc.add_paragraph()
                for line in para.splitlines():
                    if p.text:
                        p.add_run("\n")
                    p.add_run(line)

        # Footer/meta
        try:
            meta_bits = []
            if getattr(cl, "created_at", None):
                meta_bits.append(f"Created: {cl.created_at}")
            if getattr(cl, "user", None) and getattr(cl.user, "name", None):
                meta_bits.append(f"Author: {cl.user.name}")
            if meta_bits:
                doc.add_paragraph("\n".join(meta_bits))
        except Exception:
            pass

        buf = BytesIO()
        doc.save(buf)
        buf.seek(0)

        import re as _re2

        filename_parts = ["cover-letter", str(cl.id)]
        try:
            if getattr(cl, "job_post", None) and getattr(cl.job_post, "company", None):
                nm = getattr(cl.job_post.company, "name", "") or ""
                if nm:
                    filename_parts.append(_re2.sub(r"[^A-Za-z0-9_-]+", "-", nm))
        except Exception:
            pass
        filename = "-".join([p for p in filename_parts if p]) + ".docx"

        resp = HttpResponse(
            buf.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        resp["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp


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
            payload["included"] = self._build_included([exp], include_rels, request)
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
            payload["included"] = self._build_included([exp], include_rels, request)
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
            payload["included"] = self._build_included([edu], include_rels, request)
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
            payload["included"] = self._build_included([cert], include_rels, request)
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
            payload["included"] = self._build_included([desc], include_rels, request)
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
            payload["included"] = self._build_included([desc], include_rels, request)
        return Response(payload)

    @action(detail=True, methods=["get"])
    def experiences(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [ExperienceSerializer().to_resource(e) for e in (obj.experiences or [])]
        return Response({"data": data})
