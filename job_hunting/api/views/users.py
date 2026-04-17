import logging
import os

from django.contrib.auth import get_user_model
from django.conf import settings
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.parsers import JSONParser
from drf_spectacular.utils import (
    extend_schema,
    OpenApiResponse,
    inline_serializer,
)
from rest_framework import serializers as drf_serializers

from job_hunting.api.permissions import IsGuestReadOnly
from ..parsers import VndApiJSONParser
from ..serializers import (
    DjangoUserSerializer,
    ResumeSerializer,
    ScoreSerializer,
    CoverLetterSerializer,
    JobApplicationSerializer,
    SummarySerializer,
    ApiKeySerializer,
    TYPE_TO_SERIALIZER,
)
from job_hunting.lib.ai_client import set_api_key
from job_hunting.models import (
    Resume,
    Score,
    CoverLetter,
    JobApplication,
    Summary,
    ApiKey,
)
from ._schema import _INCLUDE_PARAM

logger = logging.getLogger(__name__)


_USER_RESOURCE_RESPONSE = OpenApiResponse(
    description="JSON:API user resource",
    response=inline_serializer(
        name="UserResource",
        fields={"data": drf_serializers.DictField()},
    ),
)

_USER_LIST_RESPONSE = OpenApiResponse(
    description="JSON:API list of user resources",
    response=inline_serializer(
        name="UserListResource",
        fields={
            "data": drf_serializers.ListField(child=drf_serializers.DictField()),
            "included": drf_serializers.ListField(
                child=drf_serializers.DictField(), required=False
            ),
        },
    ),
)

_USER_WRITE_REQUEST = inline_serializer(
    name="UserWriteRequest",
    fields={
        "username": drf_serializers.CharField(required=False),
        "email": drf_serializers.EmailField(required=False),
        "password": drf_serializers.CharField(required=False),
        "first_name": drf_serializers.CharField(required=False),
        "last_name": drf_serializers.CharField(required=False),
        "phone": drf_serializers.CharField(required=False),
    },
)


class DjangoUserViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated, IsGuestReadOnly]
    parser_classes = [VndApiJSONParser, JSONParser]

    def get_permissions(self):
        """Allow unauthenticated access for create and bootstrap_superuser actions."""
        if self.action in ["create", "bootstrap_superuser"]:
            return [AllowAny()]
        return super().get_permissions()

    def options(self, request, *args, **kwargs):
        # Explicitly handle CORS preflight to avoid auth and ensure proper headers
        allow_methods = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        origin = request.META.get("HTTP_ORIGIN")
        requested_headers = request.META.get("HTTP_ACCESS_CONTROL_REQUEST_HEADERS")

        resp = Response(status=200)
        resp["Allow"] = allow_methods
        resp["Access-Control-Allow-Methods"] = allow_methods
        if origin:
            resp["Access-Control-Allow-Origin"] = origin
            resp["Vary"] = "Origin"
            resp["Access-Control-Allow-Credentials"] = "true"
        else:
            resp["Access-Control-Allow-Origin"] = "*"

        if requested_headers:
            resp["Access-Control-Allow-Headers"] = requested_headers
        else:
            resp["Access-Control-Allow-Headers"] = "Authorization, Content-Type"

        resp["Access-Control-Max-Age"] = "600"
        return resp

    def get_throttles(self):
        # Disable DRF throttling in development-like environments
        try:
            debug = bool(getattr(settings, "DEBUG", False))
        except Exception:
            debug = False
        env_val = os.environ.get("ENV", "").lower()
        disable_flag = str(os.environ.get("DISABLE_THROTTLE", "")).strip().lower() in (
            "1",
            "true",
            "yes",
        )
        settings_disable = bool(getattr(settings, "DISABLE_THROTTLE", False))
        if (
            debug
            or env_val in ("dev", "development", "local")
            or disable_flag
            or settings_disable
        ):
            return []
        return super().get_throttles()

    def get_serializer(self, *args, **kwargs):
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

    @extend_schema(
        tags=["Users"],
        summary="List users (staff sees all; others see only themselves)",
        parameters=[_INCLUDE_PARAM],
        responses={200: _USER_LIST_RESPONSE},
    )
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
            payload["included"] = self._build_included(users, include_rels)
        return Response(payload)

    @extend_schema(
        tags=["Users"],
        summary="Retrieve a user by ID",
        parameters=[_INCLUDE_PARAM],
        responses={
            200: _USER_RESOURCE_RESPONSE,
            403: OpenApiResponse(description="Forbidden"),
            404: OpenApiResponse(description="Not found"),
        },
    )
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
            payload["included"] = self._build_included([user], include_rels)
        return Response(payload)

    @extend_schema(
        tags=["Users"],
        summary="Store the OpenAI API key (superuser only)",
        request=inline_serializer(
            name="SetOpenAIKeyRequest",
            fields={"openai_api_key": drf_serializers.CharField()},
        ),
        responses={
            201: OpenApiResponse(
                description="Key saved",
                response=inline_serializer(
                    name="SetOpenAIKeyResponse",
                    fields={"meta": drf_serializers.DictField()},
                ),
            ),
            400: OpenApiResponse(description="Missing or invalid key"),
            403: OpenApiResponse(description="Forbidden — superuser only"),
        },
    )
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

    @extend_schema(
        tags=["Auth"],
        summary="Register a new user",
        auth=[],
        request=_USER_WRITE_REQUEST,
        responses={
            201: _USER_RESOURCE_RESPONSE,
            400: OpenApiResponse(
                description="Validation error (missing fields, duplicate username/email)"
            ),
        },
    )
    def create(self, request):
        # Staff users can always create accounts; public registration requires REGISTRATION_OPEN
        is_staff = request.user and request.user.is_authenticated and request.user.is_staff
        if not is_staff and not settings.REGISTRATION_OPEN:
            return Response(
                {"errors": [{"detail": "Registration is currently closed."}]},
                status=403,
            )

        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response({"errors": [{"detail": str(e)}]}, status=400)

        # Extract profile fields before user creation
        profile_fields = {}
        for pf in ("phone", "linkedin", "github", "address", "links"):
            if pf in attrs:
                profile_fields[pf] = attrs.pop(pf)

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

        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError
        try:
            validate_password(password)
        except ValidationError as e:
            return Response(
                {"errors": [{"detail": msg} for msg in e.messages]}, status=400
            )

        user = User(
            username=username, email=email, first_name=first_name, last_name=last_name
        )
        user.set_password(password)
        user.save()

        # Handle profile fields via Django Profile if any provided
        if profile_fields:
            from job_hunting.models import Profile

            prof, _ = Profile.objects.get_or_create(user_id=user.id)
            if "phone" in profile_fields:
                val = str(profile_fields["phone"] or "").strip()
                prof.phone = (val[:50] or None) if val else None
            if "linkedin" in profile_fields:
                val = str(profile_fields["linkedin"] or "").strip()
                prof.linkedin = (val[:255] or None) if val else None
            if "github" in profile_fields:
                val = str(profile_fields["github"] or "").strip()
                prof.github = (val[:255] or None) if val else None
            if "address" in profile_fields:
                prof.address = (str(profile_fields["address"] or "").strip() or None)
            if "links" in profile_fields:
                val = profile_fields["links"]
                prof.links = val if isinstance(val, (dict, list)) else {}
            prof.save()

        # Send welcome email (non-blocking — don't fail registration on email error)
        if email:
            try:
                from django.core.mail import send_mail
                from django.template.loader import render_to_string

                login_url = f"{settings.FRONTEND_URL}/login"
                body = render_to_string(
                    "welcome_email.txt",
                    {"username": username, "login_url": login_url},
                )
                send_mail(
                    subject="Welcome to Career Caddy",
                    message=body,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[email],
                )
            except Exception:
                logger.warning("Failed to send welcome email to %s", email)

        return Response({"data": ser.to_resource(user)}, status=status.HTTP_201_CREATED)

    @extend_schema(
        tags=["Users"],
        summary="Replace a user (full update)",
        request=_USER_WRITE_REQUEST,
        responses={
            200: _USER_RESOURCE_RESPONSE,
            400: OpenApiResponse(description="Validation error"),
            404: OpenApiResponse(description="Not found"),
        },
    )
    def update(self, request, pk=None):
        return self._upsert(request, pk, partial=False)

    @extend_schema(
        tags=["Users"],
        summary="Partially update a user",
        request=_USER_WRITE_REQUEST,
        responses={
            200: _USER_RESOURCE_RESPONSE,
            400: OpenApiResponse(description="Validation error"),
            404: OpenApiResponse(description="Not found"),
        },
    )
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

        # Extract profile fields before user updates
        profile_fields = {}
        for pf in ("phone", "linkedin", "github", "address", "links"):
            if pf in attrs:
                profile_fields[pf] = attrs.pop(pf)

        # Update allowed fields
        if "email" in attrs:
            user.email = attrs["email"]
        if "first_name" in attrs:
            user.first_name = attrs["first_name"]
        if "last_name" in attrs:
            user.last_name = attrs["last_name"]
        if attrs.get("password"):
            from django.contrib.auth.password_validation import validate_password
            from django.core.exceptions import ValidationError
            try:
                validate_password(attrs["password"], user=user)
            except ValidationError as e:
                return Response(
                    {"errors": [{"detail": msg} for msg in e.messages]}, status=400
                )
            user.set_password(attrs["password"])

        # Staff-only fields
        if "is_staff" in attrs:
            if not request.user.is_staff:
                return Response({"errors": [{"detail": "Forbidden"}]}, status=403)
            user.is_staff = bool(attrs["is_staff"])
        if "is_active" in attrs:
            if not request.user.is_staff:
                return Response({"errors": [{"detail": "Forbidden"}]}, status=403)
            user.is_active = bool(attrs["is_active"])

        user.save()

        # Handle profile fields via Django Profile if any provided
        if profile_fields:
            from job_hunting.models import Profile

            prof, _ = Profile.objects.get_or_create(user_id=user.id)
            if "phone" in profile_fields:
                val = str(profile_fields["phone"] or "").strip()
                prof.phone = (val[:50] or None) if val else None
            if "linkedin" in profile_fields:
                val = str(profile_fields["linkedin"] or "").strip()
                prof.linkedin = (val[:255] or None) if val else None
            if "github" in profile_fields:
                val = str(profile_fields["github"] or "").strip()
                prof.github = (val[:255] or None) if val else None
            if "address" in profile_fields:
                prof.address = (str(profile_fields["address"] or "").strip() or None)
            if "links" in profile_fields:
                val = profile_fields["links"]
                prof.links = val if isinstance(val, (dict, list)) else {}
            prof.save()

        return Response({"data": ser.to_resource(user)})

    @extend_schema(
        tags=["Users"],
        summary="Delete a user (staff only)",
        responses={
            204: OpenApiResponse(description="Deleted"),
            403: OpenApiResponse(description="Forbidden — staff only"),
        },
    )
    def destroy(self, request, pk=None):
        if not request.user.is_staff:
            return Response({"errors": [{"detail": "Forbidden"}]}, status=403)
        User = get_user_model()
        try:
            user = User.objects.get(id=int(pk))
            user.delete()
        except (User.DoesNotExist, ValueError):
            pass
        return Response(status=204)

    @extend_schema(
        tags=["Auth"],
        summary="[Deprecated] Bootstrap superuser — use /api/v1/initialize/ instead",
        auth=[],
        deprecated=True,
        responses={410: OpenApiResponse(description="Gone — endpoint removed")},
    )
    @action(
        detail=False,
        methods=["post"],
        url_path="bootstrap-superuser",
        permission_classes=[AllowAny],
    )
    def bootstrap_superuser(self, request):
        """Deprecated: Use /api/v1/initialize/ instead."""
        return Response(
            {
                "errors": [
                    {
                        "detail": "This endpoint is deprecated. Use /api/v1/initialize/ instead."
                    }
                ]
            },
            status=410,  # Gone
        )

    @extend_schema(
        tags=["Users"],
        summary="List resumes for a user",
        responses={
            200: OpenApiResponse(description="JSON:API list of resume resources")
        },
    )
    @action(detail=True, methods=["get"])
    def resumes(self, request, pk=None):
        if not request.user.is_staff and int(pk) != request.user.id:
            return Response({"errors": [{"detail": "Forbidden"}]}, status=403)
        User = get_user_model()
        try:
            user = User.objects.get(id=int(pk))
        except (User.DoesNotExist, ValueError):
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        resumes = list(Resume.objects.filter(user_id=user.id))
        data = [ResumeSerializer().to_resource(r) for r in resumes]
        return Response({"data": data})

    @extend_schema(
        tags=["Users"],
        summary="List scores for a user",
        responses={
            200: OpenApiResponse(description="JSON:API list of score resources")
        },
    )
    @action(detail=True, methods=["get"])
    def scores(self, request, pk=None):
        if not request.user.is_staff and int(pk) != request.user.id:
            return Response({"errors": [{"detail": "Forbidden"}]}, status=403)
        User = get_user_model()
        try:
            user = User.objects.get(id=int(pk))
        except (User.DoesNotExist, ValueError):
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        scores = list(Score.objects.filter(user_id=user.id))
        data = [ScoreSerializer().to_resource(s) for s in scores]
        return Response({"data": data})

    @extend_schema(
        tags=["Users"],
        summary="List cover letters for a user",
        responses={
            200: OpenApiResponse(description="JSON:API list of cover-letter resources")
        },
    )
    @action(detail=True, methods=["get"], url_path="cover-letters")
    def cover_letters(self, request, pk=None):
        if not request.user.is_staff and int(pk) != request.user.id:
            return Response({"errors": [{"detail": "Forbidden"}]}, status=403)
        User = get_user_model()
        try:
            user = User.objects.get(id=int(pk))
        except (User.DoesNotExist, ValueError):
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        cover_letters = list(CoverLetter.objects.filter(user_id=user.id))
        data = [CoverLetterSerializer().to_resource(c) for c in cover_letters]
        return Response({"data": data})

    @extend_schema(
        tags=["Users"],
        summary="List job applications for a user",
        responses={
            200: OpenApiResponse(description="JSON:API list of application resources")
        },
    )
    @action(detail=True, methods=["get"], url_path="job-applications")
    def applications(self, request, pk=None):
        if not request.user.is_staff and int(pk) != request.user.id:
            return Response({"errors": [{"detail": "Forbidden"}]}, status=403)
        User = get_user_model()
        try:
            user = User.objects.get(id=int(pk))
        except (User.DoesNotExist, ValueError):
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        applications = list(JobApplication.objects.filter(user_id=user.id))
        data = [JobApplicationSerializer().to_resource(a) for a in applications]
        return Response({"data": data})

    @extend_schema(
        tags=["Users"],
        summary="List summaries for a user",
        responses={
            200: OpenApiResponse(description="JSON:API list of summary resources")
        },
    )
    @action(detail=True, methods=["get"])
    def summaries(self, request, pk=None):
        if not request.user.is_staff and int(pk) != request.user.id:
            return Response({"errors": [{"detail": "Forbidden"}]}, status=403)
        User = get_user_model()
        try:
            user = User.objects.get(id=int(pk))
        except (User.DoesNotExist, ValueError):
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        summaries = list(Summary.objects.filter(user_id=user.id))
        data = [SummarySerializer().to_resource(s) for s in summaries]
        return Response({"data": data})

    @extend_schema(
        tags=["Users"],
        summary="List API keys for a user (own keys, or any if staff)",
        responses={
            200: OpenApiResponse(description="JSON:API list of api-key resources"),
            403: OpenApiResponse(description="Forbidden"),
        },
    )
    @action(detail=True, methods=["get"], url_path="api-keys")
    def api_keys(self, request, pk=None):
        """Get API keys for a user"""
        User = get_user_model()
        try:
            user = User.objects.get(id=int(pk))
        except (User.DoesNotExist, ValueError):
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        # Only allow users to see their own API keys or staff to see any
        if not request.user.is_staff and user.id != request.user.id:
            return Response({"errors": [{"detail": "Forbidden"}]}, status=403)

        api_keys = ApiKey.objects.filter(user_id=user.id)
        data = [ApiKeySerializer().to_resource(k) for k in api_keys]
        return Response({"data": data})

    @extend_schema(
        tags=["Auth"],
        summary="Get the authenticated user's own record",
        parameters=[_INCLUDE_PARAM],
        responses={200: _USER_RESOURCE_RESPONSE},
    )
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
            payload["included"] = self._build_included([user], include_rels)
        return Response(payload)
