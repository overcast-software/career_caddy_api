import logging
import math
import threading

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
from job_hunting.lib.scoring.job_scorer import JobScorer
from job_hunting.lib.ai_client import get_client
from job_hunting.lib.services.db_export_service import DbExportService
from job_hunting.lib.services.application_prompt_builder import ApplicationPromptBuilder
from job_hunting.lib.models import CareerData
from job_hunting.models import (
    Score,
    JobPost,
    Resume,
    AiUsage,
)

logger = logging.getLogger(__name__)


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

        myJobScorer = JobScorer(client)

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
        user_id = _rel_id("user", "users")
        resume_id = _rel_id("resume", "resumes")

        if job_post_id is None:
            return Response(
                {"errors": [{"detail": "Missing required relationship: job-post"}]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        job_post_id = int(job_post_id)
        # Infer user from auth token; relationship is optional
        user_id = int(user_id) if user_id is not None else request.user.id
        # Missing or null resume defaults to career-data scoring (equivalent to resume_id=0)
        resume_id = int(resume_id) if resume_id is not None else 0

        jp = JobPost.objects.filter(pk=job_post_id).first()
        if not jp:
            return Response(
                {"errors": [{"detail": "Job post not found"}]},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not jp.description or not jp.description.strip():
            return Response(
                {"errors": [{"detail": "Job post has no description to score against"}]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        exporter = DbExportService()

        if resume_id == 0:
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

        score_id = myScore.id
        captured_description = jp.description
        captured_resume_markdown = resume_markdown

        captured_user_id = request.user.id
        captured_injected_prompt = injected_prompt

        def _score():
            import django
            django.db.close_old_connections()
            try:
                result = myJobScorer.score_job_match(captured_description, captured_resume_markdown, injected_prompt=captured_injected_prompt)
                Score.objects.filter(pk=score_id).update(
                    score=result.score,
                    explanation=result.evaluation,
                    status="completed",
                )
            except Exception:
                logger.exception("Scoring failed for score_id=%s", score_id)
                Score.objects.filter(pk=score_id).update(status="failed")
                return
            # Record AI usage — separate try so a logging failure doesn't mark the score as failed
            try:
                usage = getattr(result, "_usage", None)
                model_name = getattr(result, "_model_name", "unknown")
                if usage:
                    AiUsage.objects.create(
                        user_id=captured_user_id,
                        agent_name="job_scorer",
                        model_name=model_name,
                        trigger="score",
                        request_tokens=usage.request_tokens or 0,
                        response_tokens=usage.response_tokens or 0,
                        total_tokens=usage.total_tokens or 0,
                        request_count=usage.requests or 1,
                    )
                else:
                    logger.warning("No usage data for score_id=%s", score_id)
            except Exception:
                logger.exception("Failed to record AI usage for score_id=%s", score_id)

        threading.Thread(target=_score, daemon=True).start()

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

