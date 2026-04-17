import logging
import math

from django.contrib.auth import get_user_model
from django.conf import settings
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from rest_framework.response import Response
from drf_spectacular.utils import (
    extend_schema,
    OpenApiResponse,
    inline_serializer,
)
from rest_framework import serializers as drf_serializers

from .base import BaseViewSet
from ._schema import (
    _PAGE_PARAMS,
    _JSONAPI_LIST,
    _JSONAPI_ITEM,
)
from ..serializers import (
    ApiKeySerializer,
    AiUsageSerializer,
    WaitlistSerializer,
    InvitationSerializer,
)
from job_hunting.api.permissions import IsGuestReadOnly
from job_hunting.models import (
    ApiKey,
    AiUsage,
    Waitlist,
    Invitation,
)

logger = logging.getLogger(__name__)


class ApiKeyViewSet(BaseViewSet):
    model = ApiKey
    serializer_class = ApiKeySerializer
    permission_classes = [IsAuthenticated, IsGuestReadOnly]

    @extend_schema(
        tags=["API Keys"],
        summary="List API keys (staff sees all; others see only their own)",
        parameters=_PAGE_PARAMS,
        responses={200: _JSONAPI_LIST},
    )
    def list(self, request):
        """List API keys - all keys for admins, user's own keys for regular users"""
        if request.user.is_staff:
            items = list(ApiKey.objects.all())
        else:
            items = list(ApiKey.objects.filter(user_id=request.user.id))

        items = self.paginate(items)
        ser = self.get_serializer()
        data = [ser.to_resource(o) for o in items]
        payload = {"data": data}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included(items, include_rels, request)
        return Response(payload)

    @extend_schema(
        tags=["API Keys"],
        summary="Retrieve an API key (own key, or any if staff)",
        responses={
            200: _JSONAPI_ITEM,
            403: OpenApiResponse(description="Forbidden"),
            404: OpenApiResponse(description="Not found"),
        },
    )
    def retrieve(self, request, pk=None):
        """Get a specific API key"""
        obj = ApiKey.objects.filter(pk=pk).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        # Verify ownership or admin access
        if not request.user.is_staff and obj.user_id != request.user.id:
            return Response({"errors": [{"detail": "Forbidden"}]}, status=403)

        ser = self.get_serializer()
        payload = {"data": ser.to_resource(obj)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([obj], include_rels, request)
        return Response(payload)

    @extend_schema(
        tags=["API Keys"],
        summary="Create an API key — plain key returned once in response attributes.key",
        request=inline_serializer(
            name="ApiKeyCreateRequest",
            fields={
                "name": drf_serializers.CharField(
                    help_text="Human-readable name for this key"
                ),
                "expires_days": drf_serializers.IntegerField(
                    required=False, help_text="Days until expiry (omit for no expiry)"
                ),
                "scopes": drf_serializers.ListField(
                    child=drf_serializers.CharField(),
                    required=False,
                    help_text="Defaults to ['read', 'write']",
                ),
            },
        ),
        responses={
            201: OpenApiResponse(
                description="API key created — includes plain key in attributes.key (only shown once)"
            ),
            400: OpenApiResponse(description="Missing name or invalid user"),
        },
    )
    def create(self, request):
        """Create a new API key"""
        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") or {}
        attrs = node.get("attributes") or {}
        relationships = node.get("relationships") or {}

        name = attrs.get("name")
        if not name:
            return Response(
                {"errors": [{"detail": "Name is required"}]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        expires_days = attrs.get("expires_days")
        scopes = attrs.get("scopes", ["read", "write"])  # Default scopes

        # Determine target user - admins can create keys for other users
        target_user_id = request.user.id  # Default to current user

        # Check if admin is specifying a different user
        user_rel = relationships.get("user") or relationships.get("users")
        if user_rel and request.user.is_staff:
            user_data = user_rel.get("data")
            if isinstance(user_data, dict) and "id" in user_data:
                try:
                    target_user_id = int(user_data["id"])
                    # Verify the target user exists
                    User = get_user_model()
                    if not User.objects.filter(id=target_user_id).exists():
                        return Response(
                            {"errors": [{"detail": "Invalid user ID"}]},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                except (TypeError, ValueError):
                    return Response(
                        {"errors": [{"detail": "Invalid user ID"}]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
        elif user_rel and not request.user.is_staff:
            return Response(
                {
                    "errors": [
                        {"detail": "Only admins can create API keys for other users"}
                    ]
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            api_key_obj, plain_key = ApiKey.generate_key(
                name=name,
                user_id=target_user_id,
                expires_days=expires_days,
                scopes=scopes,
            )
        except Exception as e:
            return Response(
                {"errors": [{"detail": f"Failed to create API key: {str(e)}"}]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ser = self.get_serializer()
        resource = ser.to_resource(api_key_obj)

        # Include the plain key in the response (only time it's available)
        resource["attributes"]["key"] = plain_key

        payload = {"data": resource}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included(
                [api_key_obj], include_rels, request
            )

        return Response(payload, status=status.HTTP_201_CREATED)

    @extend_schema(
        tags=["API Keys"],
        summary="Revoke (delete) an API key",
        responses={
            204: OpenApiResponse(description="Revoked"),
            403: OpenApiResponse(description="Forbidden"),
        },
    )
    def destroy(self, request, pk=None):
        """Revoke an API key"""
        obj = ApiKey.objects.filter(pk=pk).first()
        if not obj:
            return Response(status=204)

        # Verify ownership or admin access
        if not request.user.is_staff and obj.user_id != request.user.id:
            return Response({"errors": [{"detail": "Forbidden"}]}, status=403)

        obj.delete()
        return Response(status=204)

    @extend_schema(
        tags=["API Keys"],
        summary="Revoke an API key (alternative to DELETE)",
        responses={
            200: _JSONAPI_ITEM,
            403: OpenApiResponse(description="Forbidden"),
            404: OpenApiResponse(description="Not found"),
        },
    )
    @action(detail=True, methods=["post"])
    def revoke(self, request, pk=None):
        """Revoke an API key (alternative to DELETE)"""
        obj = ApiKey.objects.filter(pk=pk).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        # Verify ownership or admin access
        if not request.user.is_staff and obj.user_id != request.user.id:
            return Response({"errors": [{"detail": "Forbidden"}]}, status=403)

        obj.revoke()

        ser = self.get_serializer()
        payload = {"data": ser.to_resource(obj)}
        return Response(payload)


class AiUsageViewSet(BaseViewSet):
    model = AiUsage
    serializer_class = AiUsageSerializer

    def _base_queryset(self, request):
        if request.user.is_staff:
            qs = AiUsage.objects.all()
            user_id = request.query_params.get("user_id")
            if user_id:
                qs = qs.filter(user_id=int(user_id))
            return qs
        return AiUsage.objects.filter(user=request.user)

    def _apply_filters(self, qs, request):
        for field in ("agent_name", "model_name", "trigger"):
            val = request.query_params.get(field)
            if val:
                qs = qs.filter(**{field: val})
        pipeline_run_id = request.query_params.get("pipeline_run_id")
        if pipeline_run_id:
            qs = qs.filter(pipeline_run_id=pipeline_run_id)
        date_from = request.query_params.get("date_from")
        if date_from:
            qs = qs.filter(created_at__gte=date_from)
        date_to = request.query_params.get("date_to")
        if date_to:
            qs = qs.filter(created_at__lte=date_to)
        return qs

    def list(self, request):
        qs = self._base_queryset(request)
        qs = self._apply_filters(qs, request)
        qs = qs.order_by("-created_at")

        total = qs.count()
        page_number, page_size = self._page_params()
        total_pages = math.ceil(total / page_size) if page_size else 1
        offset = (page_number - 1) * page_size
        items = list(qs[offset:offset + page_size])

        ser = self.get_serializer()
        data = [ser.to_resource(o) for o in items]
        payload = {
            "data": data,
            "meta": {
                "total": total,
                "page": page_number,
                "per_page": page_size,
                "total_pages": total_pages,
            },
            "links": {"next": None},
        }

        if page_number < total_pages:
            base = request.build_absolute_uri(request.path)
            qp = request.query_params.dict()
            qp["page"] = page_number + 1
            qp["per_page"] = page_size
            payload["links"]["next"] = base + "?" + "&".join(
                f"{k}={v}" for k, v in qp.items()
            )

        return Response(payload)

    def create(self, request):
        from job_hunting.lib.pricing import estimate_cost

        data = request.data if isinstance(request.data, dict) else {}
        ser = self.get_serializer()

        if "data" in data:
            try:
                attrs = ser.parse_payload(request.data)
            except ValueError as e:
                return Response({"errors": [{"detail": str(e)}]}, status=400)
        else:
            attrs = {k: data[k] for k in data if k in {
                "agent_name", "model_name", "trigger", "pipeline_run_id",
                "request_tokens", "response_tokens", "total_tokens", "request_count",
            }}

        attrs["user_id"] = request.user.id
        attrs.pop("estimated_cost_usd", None)
        attrs["estimated_cost_usd"] = estimate_cost(
            attrs.get("model_name", ""),
            int(attrs.get("request_tokens", 0)),
            int(attrs.get("response_tokens", 0)),
        )

        obj = AiUsage.objects.create(**attrs)
        return Response({"data": ser.to_resource(obj)}, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        from django.db.models import Sum, Count
        from django.db.models.functions import TruncDay, TruncWeek, TruncMonth

        qs = self._base_queryset(request)
        qs = self._apply_filters(qs, request)

        days = int(request.query_params.get("days", 30))
        from django.utils import timezone as tz
        cutoff = tz.now() - tz.timedelta(days=days)
        qs = qs.filter(created_at__gte=cutoff)

        period = request.query_params.get("period", "daily")
        trunc_fn = {"daily": TruncDay, "weekly": TruncWeek, "monthly": TruncMonth}.get(
            period, TruncDay
        )

        group_by = request.query_params.get("group_by")
        valid_groups = {"agent_name", "model_name", "trigger"}
        group_field = group_by if group_by in valid_groups else None

        values_fields = ["period"]
        if group_field:
            values_fields.append(group_field)

        buckets = (
            qs.annotate(period=trunc_fn("created_at"))
            .values(*values_fields)
            .annotate(
                total_tokens=Sum("total_tokens"),
                request_tokens=Sum("request_tokens"),
                response_tokens=Sum("response_tokens"),
                estimated_cost_usd=Sum("estimated_cost_usd"),
                request_count=Count("id"),
            )
            .order_by("period")
        )

        bucket_list = []
        for b in buckets:
            entry = {
                "period": b["period"].isoformat() if b["period"] else None,
                "total_tokens": b["total_tokens"] or 0,
                "request_tokens": b["request_tokens"] or 0,
                "response_tokens": b["response_tokens"] or 0,
                "estimated_cost_usd": str(b["estimated_cost_usd"] or 0),
                "request_count": b["request_count"],
            }
            if group_field:
                entry[group_field] = b[group_field]
            bucket_list.append(entry)

        totals = qs.aggregate(
            total_tokens=Sum("total_tokens"),
            request_tokens=Sum("request_tokens"),
            response_tokens=Sum("response_tokens"),
            estimated_cost_usd=Sum("estimated_cost_usd"),
            request_count=Count("id"),
        )

        return Response({
            "data": {
                "buckets": bucket_list,
                "totals": {
                    "total_tokens": totals["total_tokens"] or 0,
                    "request_tokens": totals["request_tokens"] or 0,
                    "response_tokens": totals["response_tokens"] or 0,
                    "estimated_cost_usd": str(totals["estimated_cost_usd"] or 0),
                    "request_count": totals["request_count"],
                },
            },
        })


class WaitlistViewSet(BaseViewSet):
    model = Waitlist
    serializer_class = WaitlistSerializer
    permission_classes = [IsAuthenticated, IsAdminUser]

    def list(self, request):
        qs = Waitlist.objects.all().order_by("-created_at")
        total = qs.count()
        page_number, page_size = self._page_params()
        total_pages = math.ceil(total / page_size) if page_size else 1
        offset = (page_number - 1) * page_size
        items = list(qs[offset: offset + page_size])
        ser = self.get_serializer()
        return Response({
            "data": [ser.to_resource(o) for o in items],
            "meta": {"total": total, "page": page_number, "per_page": page_size, "total_pages": total_pages},
        })

    def destroy(self, request, pk=None):
        obj = Waitlist.objects.filter(pk=pk).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        obj.delete()
        return Response(status=204)


class InvitationViewSet(BaseViewSet):
    model = Invitation
    serializer_class = InvitationSerializer
    permission_classes = [IsAuthenticated, IsAdminUser]

    def list(self, request):
        qs = Invitation.objects.all().order_by("-created_at")
        total = qs.count()
        page_number, page_size = self._page_params()
        total_pages = math.ceil(total / page_size) if page_size else 1
        offset = (page_number - 1) * page_size
        items = list(qs[offset: offset + page_size])
        ser = self.get_serializer()
        return Response({
            "data": [ser.to_resource(o) for o in items],
            "meta": {"total": total, "page": page_number, "per_page": page_size, "total_pages": total_pages},
        })

    def create(self, request):
        import json
        import secrets
        from django.utils import timezone as tz
        from django.core.mail import send_mail
        from django.template.loader import render_to_string

        try:
            payload = request.data
            if isinstance(payload, bytes):
                payload = json.loads(payload.decode("utf-8") or "{}")
        except Exception:
            payload = {}

        # Support both JSON:API envelope and flat body
        if "data" in payload and isinstance(payload["data"], dict):
            attrs = payload["data"].get("attributes", {})
        else:
            attrs = payload

        email = (attrs.get("email") or "").strip().lower()
        if not email or "@" not in email:
            return Response(
                {"errors": [{"detail": "A valid email address is required."}]},
                status=400,
            )

        token = secrets.token_urlsafe(32)
        expires_at = tz.now() + tz.timedelta(days=7)

        invitation = Invitation.objects.create(
            email=email,
            token=token,
            created_by=request.user,
            expires_at=expires_at,
        )

        # Auto-remove matching waitlist entry
        Waitlist.objects.filter(email=email).delete()

        # Send invitation email
        try:
            invite_url = f"{settings.FRONTEND_URL}/accept-invite?token={token}"
            body = render_to_string(
                "invitation_email.txt", {"invite_url": invite_url}
            )
            send_mail(
                subject="You're invited to Career Caddy",
                message=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[email],
            )
        except Exception:
            logger.warning("Failed to send invitation email to %s", email)

        ser = self.get_serializer()
        return Response(
            {"data": ser.to_resource(invitation)}, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"], url_path="resend")
    def resend(self, request, pk=None):
        from django.utils import timezone as tz
        from django.core.mail import send_mail
        from django.template.loader import render_to_string

        obj = Invitation.objects.filter(pk=pk).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        if obj.is_accepted:
            return Response(
                {"errors": [{"detail": "Invitation already accepted."}]}, status=400
            )

        # Reset expiry if expired
        if obj.is_expired:
            obj.expires_at = tz.now() + tz.timedelta(days=7)
            obj.save()

        invite_url = f"{settings.FRONTEND_URL}/accept-invite?token={obj.token}"
        body = render_to_string(
            "invitation_email.txt", {"invite_url": invite_url}
        )
        try:
            send_mail(
                subject="You're invited to Career Caddy",
                message=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[obj.email],
            )
        except Exception:
            logger.warning("Failed to resend invitation email to %s", obj.email)
            return Response(
                {"errors": [{"detail": "Failed to send email."}]}, status=502
            )

        ser = self.get_serializer()
        return Response({"data": ser.to_resource(obj)})

    def destroy(self, request, pk=None):
        obj = Invitation.objects.filter(pk=pk).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        obj.delete()
        return Response(status=204)

