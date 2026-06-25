import logging
import math

from django_q.tasks import async_task
from rest_framework import status
from rest_framework.response import Response
from drf_spectacular.utils import (
    extend_schema,
    extend_schema_view,
    OpenApiResponse,
)

from .base import BaseViewSet
from ._schema import _JSONAPI_ITEM, _JSONAPI_WRITE
from ..serializers import ScoreSerializer
from job_hunting.lib.ai_client import get_client
from job_hunting.lib.services.db_export_service import DbExportService
from job_hunting.lib.services.application_prompt_builder import ApplicationPromptBuilder
from job_hunting.lib.models import CareerData
from job_hunting.models import (
    Score,
    JobPost,
    Resume,
)

logger = logging.getLogger(__name__)


def _auto_score_job_post(job_post_id: int, user_id: int) -> bool:
    """Create a pending Score and enqueue a scoring task for job_post_id.

    Returns True if the task was enqueued. Silent no-op (False) when
    preconditions aren't met: no AI client, missing description, or no career data.
    Never raises — callers wrap this in try/except anyway but this keeps the
    contract clean.

    The actual scoring work runs in the qcluster worker via
    job_hunting.lib.tasks.score_job. The view's job is row bookkeeping +
    enqueue; the task re-fetches and runs the LLM.
    """
    client = get_client(required=False)
    if client is None:
        return False

    jp = JobPost.objects.filter(pk=job_post_id).first()
    if not jp or not jp.description or not jp.description.strip():
        return False

    career_data = CareerData.for_user(user_id)
    prompt_builder = ApplicationPromptBuilder(max_section_chars=60000)
    resume_markdown = prompt_builder.build_from_career_data(career_data)
    if not resume_markdown.strip():
        return False

    my_score = Score.objects.filter(
        job_post_id=job_post_id, resume_id=None, user_id=user_id
    ).first()
    if my_score:
        my_score.status = "pending"
        my_score.score = None
        my_score.explanation = None
        my_score.save()
    else:
        my_score = Score.objects.create(
            job_post_id=job_post_id,
            resume_id=None,
            user_id=user_id,
            status="pending",
        )

    async_task(
        "job_hunting.lib.tasks.score_job",
        my_score.id,
        trigger="auto_score",
    )
    return True


@extend_schema_view(
    list=extend_schema(tags=["Scores"], summary="List scores"),
    retrieve=extend_schema(tags=["Scores"], summary="Retrieve a score"),
    update=extend_schema(tags=["Scores"], summary="Update a score"),
    partial_update=extend_schema(tags=["Scores"], summary="Partially update a score"),
    destroy=extend_schema(tags=["Scores"], summary="Delete a score"),
)
class ScoreViewSet(BaseViewSet):
    model = Score
    serializer_class = ScoreSerializer

    @extend_schema(
        tags=["Scores"],
        summary="AI-score a job post against a resume — returns immediately with status=pending; poll for status=completed",
        request=_JSONAPI_WRITE,
        responses={
            202: _JSONAPI_ITEM,
            400: OpenApiResponse(description="Missing required relationships or no career data"),
            503: OpenApiResponse(description="AI client not configured"),
        },
    )
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

        node = data.get("data") or {}
        relationships = node.get("relationships") or {}
        attrs_node = node.get("attributes") or {}

        # Extract injected prompt for AI scoring
        injected_prompt = (
            attrs_node.get("instructions") or attrs_node.get("injected_prompt")
            or data.get("instructions") or data.get("injected_prompt")
        )
        injected_prompt = injected_prompt.strip() if isinstance(injected_prompt, str) else None
        injected_prompt = injected_prompt or None

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
        raw_user_id = _rel_id("user", "users")
        resume_id = _rel_id("resume", "resumes")

        if job_post_id is None:
            return Response(
                {"errors": [{"detail": "Missing required relationship: job-post"}]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # job_post_id is the JobPost PK — a NanoID string (CC-57), not cast.
        # Infer user from auth token; relationship is optional
        user_id = int(raw_user_id) if raw_user_id is not None else request.user.id
        # resume_id is the Resume NanoID PK (CC-77 #79); missing/null/"0"
        # defaults to career-data scoring (resume IS NULL).
        resume_id = resume_id if resume_id not in (None, "", "0", 0) else None

        jp = JobPost.objects.filter(pk=job_post_id).first()
        if not jp:
            return Response(
                {"errors": [{"detail": "Job post not found"}]},
                status=status.HTTP_404_NOT_FOUND,
            )

        # When a staff service-account caller (e.g. score_poller) omits the
        # user relationship, infer the target user from the job post's creator
        # so the score lands under the correct user, not the daemon's account.
        if raw_user_id is None and request.user.is_staff and jp.created_by_id:
            user_id = jp.created_by_id

        if not jp.description or not jp.description.strip():
            return Response(
                {"errors": [{"detail": "Job post has no description to score against"}]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        exporter = DbExportService()

        if not resume_id:
            # Score against the user's full career data (all favorite resumes, cover letters, answers)
            career_data = CareerData.for_user(user_id)
            prompt_builder = ApplicationPromptBuilder(max_section_chars=60000)
            resume_markdown = prompt_builder.build_from_career_data(career_data)
            if not resume_markdown.strip():
                return Response(
                    {"errors": [{"detail": "No career data found for this user to score against"}]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            score_resume_id = None
        else:
            resume = Resume.objects.filter(pk=resume_id).first()
            if not resume:
                return Response(
                    {"errors": [{"detail": "Resume not found"}]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            resume_markdown = exporter.resume_markdown_export(resume)
            score_resume_id = resume_id

        myScore = Score.objects.filter(
            job_post_id=job_post_id, resume_id=score_resume_id, user_id=user_id
        ).first()
        if myScore:
            myScore.status = "pending"
            myScore.score = None
            myScore.explanation = None
            myScore.save()
        else:
            myScore = Score.objects.create(
                job_post_id=job_post_id,
                resume_id=score_resume_id,
                user_id=user_id,
                status="pending",
            )

        async_task(
            "job_hunting.lib.tasks.score_job",
            myScore.id,
            injected_prompt=injected_prompt,
            trigger="score",
        )

        ser = self.get_serializer()
        payload = {"data": ser.to_resource(myScore)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([myScore], include_rels, request)
        return Response(payload, status=status.HTTP_202_ACCEPTED)

    def _parse_eval(self, e):
        # Expect a structured result from JobScorer: dict or JSON string
        data = None
        if isinstance(e, dict):
            data = e
        else:
            try:
                import json as _json

                data = _json.loads(str(e))
            except Exception:
                return None, str(e)

        s = data.get("score")
        explanation = (
            data.get("explanation") or data.get("evaluation") or data.get("explination")
        )
        try:
            s_int = int(s) if s is not None else None
        except (TypeError, ValueError):
            s_int = None

        if s_int is None or not (1 <= s_int <= 100):
            return None, str(explanation or "").strip() or str(e)

        return s_int, str(explanation or "").strip()

    def list(self, request):
        qs = Score.objects.filter(user_id=request.user.id).order_by("-created_at", "-id")
        job_post_id = request.query_params.get("filter[job_post_id]")
        if job_post_id:
            try:
                qs = qs.filter(job_post_id=job_post_id)
            except (TypeError, ValueError):
                pass
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

    def retrieve(self, request, pk=None):
        obj = Score.objects.filter(pk=pk).first()
        if not obj or obj.user_id != request.user.id:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer()
        return Response({"data": ser.to_resource(obj)})

    def destroy(self, request, pk=None):
        obj = Score.objects.filter(pk=pk).first()
        if not obj or obj.user_id != request.user.id:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        obj.delete()
        return Response(status=204)

