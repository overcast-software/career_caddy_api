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
from ..serializers import SummarySerializer
from job_hunting.lib.ai_client import get_client
from job_hunting.lib.services.summary_service import SummaryService
from job_hunting.lib.models import CareerData
from job_hunting.lib.services.application_prompt_builder import ApplicationPromptBuilder
from job_hunting.models import (
    Summary,
    Resume,
    JobPost,
    ResumeSummary,
)


@extend_schema_view(
    list=extend_schema(tags=["Summaries"], summary="List summaries"),
    retrieve=extend_schema(tags=["Summaries"], summary="Retrieve a summary"),
    update=extend_schema(tags=["Summaries"], summary="Update a summary"),
    partial_update=extend_schema(
        tags=["Summaries"], summary="Partially update a summary"
    ),
    destroy=extend_schema(tags=["Summaries"], summary="Delete a summary"),
)
class SummaryViewSet(BaseViewSet):
    model = Summary
    serializer_class = SummarySerializer



    def list(self, request):
        qs = Summary.objects.filter(user_id=request.user.id)

        query_filter = request.query_params.get("filter[query]")
        if query_filter:
            qs = qs.filter(content__icontains=query_filter)

        qs = qs.order_by("-id")
        total = qs.count()
        page_number, page_size = self._page_params()
        total_pages = math.ceil(total / page_size) if page_size else 1
        offset = (page_number - 1) * page_size
        items = list(qs.all()[offset: offset + page_size])

        ser = self.get_serializer()
        payload = {
            "data": [ser.to_resource(obj) for obj in items],
            "meta": {"total": total, "page": page_number, "per_page": page_size, "total_pages": total_pages},
        }
        if page_number < total_pages:
            base = request.build_absolute_uri(request.path)
            qp = request.query_params.dict()
            qp["page"] = page_number + 1
            qp["per_page"] = page_size
            payload["links"] = {"next": base + "?" + "&".join(f"{k}={v}" for k, v in qp.items())}
        else:
            payload["links"] = {"next": None}
        return Response(payload)

    def retrieve(self, request, pk=None):
        obj = Summary.objects.filter(pk=pk).first()
        if not obj or obj.user_id != request.user.id:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer()
        return Response({"data": ser.to_resource(obj)})

    def update(self, request, pk=None):
        return self._update_summary(request, pk)

    def partial_update(self, request, pk=None):
        return self._update_summary(request, pk)

    def _update_summary(self, request, pk):
        obj = Summary.objects.filter(pk=pk).first()
        if not obj or obj.user_id != request.user.id:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") or {}
        attrs = node.get("attributes") or {}
        relationships = node.get("relationships") or {}

        # Update Summary fields
        if "content" in attrs:
            obj.content = attrs["content"]
        if "status" in attrs:
            obj.status = attrs["status"]
        obj.save()

        # Update ResumeSummary.active if provided
        if "active" in attrs:
            active_val = bool(attrs["active"])

            # Resolve resume_id: from relationship or flat attribute
            resume_id = attrs.get("resume_id")
            if resume_id is None:
                resume_rel = relationships.get("resume") or relationships.get("resumes")
                if isinstance(resume_rel, dict):
                    rel_data = resume_rel.get("data")
                    if isinstance(rel_data, dict):
                        resume_id = rel_data.get("id")

            if resume_id is not None:
                try:
                    resume_id = int(resume_id)
                except (TypeError, ValueError):
                    resume_id = None

            # Fall back to the single linked resume if unambiguous
            if resume_id is None:
                linked = list(ResumeSummary.objects.filter(summary_id=obj.id).values_list("resume_id", flat=True))
                if len(linked) == 1:
                    resume_id = linked[0]

            if resume_id is not None:
                ResumeSummary.objects.get_or_create(resume_id=resume_id, summary_id=obj.id)
                if active_val:
                    ResumeSummary.objects.filter(resume_id=resume_id).update(active=False)
                    ResumeSummary.objects.filter(resume_id=resume_id, summary_id=obj.id).update(active=True)
                else:
                    ResumeSummary.objects.filter(resume_id=resume_id, summary_id=obj.id).update(active=False)

        ser = self.get_serializer()
        return Response({"data": ser.to_resource(obj)})

    def destroy(self, request, pk=None):
        obj = Summary.objects.filter(pk=pk).first()
        if not obj or obj.user_id != request.user.id:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        tags=["Summaries"],
        summary="Create a summary (or AI-generate if content omitted and job-post provided)",
        request=_JSONAPI_WRITE,
        responses={
            201: _JSONAPI_ITEM,
            400: OpenApiResponse(description="Missing resume or invalid IDs"),
            503: OpenApiResponse(description="AI client not configured"),
        },
    )
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

        # Resume is optional — omitting it or passing id=0 falls back to career-data
        resume = None
        if resume_id is not None:
            try:
                rid = int(resume_id)
            except (TypeError, ValueError):
                rid = None
            if rid:  # 0 → treated as "no resume" → career-data fallback
                resume = Resume.objects.filter(pk=rid).first()
                if not resume:
                    return Response(
                        {"errors": [{"detail": "Invalid resume ID"}]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

        job_post = None
        if job_post_id is not None:
            try:
                job_post = JobPost.objects.filter(pk=int(job_post_id)).first()
            except (TypeError, ValueError):
                pass
            if not job_post:
                return Response(
                    {"errors": [{"detail": "Invalid job-post ID"}]},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        user_id = request.user.id

        content = attrs.get("content")

        # Extract injected prompt for AI generation
        injected_prompt = attrs.get("instructions") or attrs.get("injected_prompt")
        injected_prompt = injected_prompt.strip() if isinstance(injected_prompt, str) else None
        injected_prompt = injected_prompt or None

        if content:
            # Manual content — create synchronously, already complete
            summary = Summary.objects.create(
                job_post_id=job_post.id if job_post else None,
                user_id=user_id,
                content=content,
                status="completed",
            )
            if resume is not None:
                ResumeSummary.objects.filter(resume_id=resume.id).update(active=False)
                ResumeSummary.objects.get_or_create(
                    resume_id=resume.id, summary_id=summary.id, defaults={"active": True}
                )
                ResumeSummary.objects.filter(resume_id=resume.id, summary_id=summary.id).update(
                    active=True
                )
                ResumeSummary.ensure_single_active_for_resume(resume.id)
            ser = self.get_serializer()
            payload = {"data": ser.to_resource(summary)}
            include_rels = self._parse_include(request)
            if include_rels:
                payload["included"] = self._build_included([summary], include_rels, request)
            return Response(payload, status=status.HTTP_201_CREATED)

        # AI generation path — create a pending record and dispatch async
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

        career_markdown = None
        if resume is None:
            career_data = CareerData.for_user(user_id)
            prompt_builder = ApplicationPromptBuilder(max_section_chars=60000)
            career_markdown = prompt_builder.build_from_career_data(career_data)
            if not career_markdown.strip():
                return Response(
                    {"errors": [{"detail": "No career data found for this user. Add favorite resumes or provide a resume relationship."}]},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        summary = Summary.objects.create(
            job_post_id=job_post.id,
            user_id=user_id,
            status="pending",
        )
        summary_id = summary.id
        captured_injected_prompt = injected_prompt
        captured_resume = resume
        captured_career_markdown = career_markdown

        def _generate():
            import django
            django.db.close_old_connections()
            try:
                if captured_resume is None:
                    svc = SummaryService(client, job=job_post, resume_markdown=captured_career_markdown, user_id=user_id)
                else:
                    svc = SummaryService(client, job=job_post, resume=captured_resume)
                generated_content = svc.generate_content(injected_prompt=captured_injected_prompt)
                Summary.objects.filter(pk=summary_id).update(content=generated_content, status="completed")
                if captured_resume is not None:
                    ResumeSummary.objects.filter(resume_id=captured_resume.id).update(active=False)
                    ResumeSummary.objects.get_or_create(
                        resume_id=captured_resume.id, summary_id=summary_id, defaults={"active": True}
                    )
                    ResumeSummary.objects.filter(resume_id=captured_resume.id, summary_id=summary_id).update(active=True)
                    ResumeSummary.ensure_single_active_for_resume(captured_resume.id)
            except Exception:
                Summary.objects.filter(pk=summary_id).update(status="failed")

        threading.Thread(target=_generate, daemon=True).start()

        ser = self.get_serializer()
        return Response({"data": ser.to_resource(summary)}, status=status.HTTP_202_ACCEPTED)
