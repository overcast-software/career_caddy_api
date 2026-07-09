import logging

from django_q.tasks import async_task
from rest_framework import status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from drf_spectacular.utils import (
    extend_schema,
    extend_schema_view,
    OpenApiResponse,
)

from .base import BaseViewSet
from ._schema import _JSONAPI_ITEM, _JSONAPI_WRITE
from ..serializers import MatchRequestSerializer
from job_hunting.lib.url_policy import validate_submission_url, UrlPolicyError
from job_hunting.models import MatchRequest
from job_hunting.models.match_request import TEXT_EXCERPT_MAX_LEN

logger = logging.getLogger(__name__)

MATCH_TASK = "job_hunting.lib.tasks.match_request_job"


@extend_schema_view(
    retrieve=extend_schema(
        tags=["Match Requests"],
        summary="Retrieve a match request (creator or staff only)",
    ),
)
class MatchRequestViewSet(BaseViewSet):
    """Staff-gated agentic JobPost lookup (CC-135).

    The ccsender extension POSTs the application-page context here when it
    can't match the page to a JobPost deterministically; an async task makes
    ONE LLM call that picks the matching post from the caller's visible corpus
    (or null), and the extension polls the row for the result.

    Entitlement is currently ``is_staff`` (``IsAdminUser``): anonymous callers
    get 401, authenticated non-staff get 403. A per-user entitlement flag is
    the follow-up generalization. Read access is further narrowed to the
    creating user (or staff) in ``retrieve``.
    """

    model = MatchRequest
    serializer_class = MatchRequestSerializer
    permission_classes = [IsAdminUser]

    @extend_schema(
        tags=["Match Requests"],
        summary=(
            "Submit application-page context for an agentic JobPost lookup — "
            "returns 202 with status=pending; poll GET for the result"
        ),
        request=_JSONAPI_WRITE,
        responses={
            202: _JSONAPI_ITEM,
            400: OpenApiResponse(description="Missing/invalid url"),
        },
    )
    def create(self, request):
        # Accept both a JSON:API `{data: {attributes: {...}}}` envelope and a
        # flat `{url, referrer, ...}` body — the extension may send either.
        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") if isinstance(data.get("data"), dict) else None
        attrs = (node.get("attributes") if node else None) or data

        url = (attrs.get("url") or "").strip()
        referrer = (attrs.get("referrer") or "").strip()
        page_title = (attrs.get("page_title") or "").strip()
        text_excerpt = attrs.get("text_excerpt") or ""

        if not url:
            return Response(
                {"errors": [{"detail": "url is required"}]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            url = validate_submission_url(url)
        except UrlPolicyError as e:
            return Response(
                {"errors": [{"status": "400", "code": e.code, "detail": str(e)}]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Referrer is a best-effort signal — validate but never 4xx on a bad
        # one; drop it so a malformed referrer can't block the lookup.
        if referrer:
            try:
                referrer = validate_submission_url(referrer)
            except UrlPolicyError:
                referrer = ""

        # The row itself is the token-cost guardrail: truncate at write so the
        # matcher never sees more than TEXT_EXCERPT_MAX_LEN chars.
        if not isinstance(text_excerpt, str):
            text_excerpt = ""
        text_excerpt = text_excerpt[:TEXT_EXCERPT_MAX_LEN]

        match_request = MatchRequest.objects.create(
            created_by=request.user,
            url=url,
            referrer=referrer,
            page_title=page_title[:500],
            text_excerpt=text_excerpt,
        )

        async_task(MATCH_TASK, match_request.id)

        ser = self.get_serializer(request=request)
        return Response(
            {"data": ser.to_resource(match_request)},
            status=status.HTTP_202_ACCEPTED,
        )

    def retrieve(self, request, pk=None):
        obj = MatchRequest.objects.filter(pk=pk).first()
        # Creator or staff only. A non-owning staff caller can still read it
        # (support/debug); everyone else gets 404, not 403, so the existence of
        # another user's row isn't leaked.
        if not obj or (obj.created_by_id != request.user.id and not request.user.is_staff):
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer(request=request)
        payload = {"data": ser.to_resource(obj)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([obj], include_rels, request)
        return Response(payload)
