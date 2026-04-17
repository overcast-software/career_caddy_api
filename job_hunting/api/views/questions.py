import logging
import math

from django.db.models import Q
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from drf_spectacular.utils import (
    extend_schema,
    extend_schema_view,
    OpenApiResponse,
    inline_serializer,
)
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
    QuestionSerializer,
    AnswerSerializer,
)
from job_hunting.lib.ai_client import get_client
from job_hunting.lib.services.db_export_service import DbExportService
from job_hunting.lib.services.answer_service import AnswerService
from job_hunting.lib.services.application_prompt_builder import ApplicationPromptBuilder
from job_hunting.lib.models import CareerData
from job_hunting.models import (
    Question,
    Answer,
    JobApplication,
    Resume,
)

logger = logging.getLogger(__name__)


@extend_schema_view(
    update=extend_schema(tags=["Questions"], summary="Update a question"),
    partial_update=extend_schema(
        tags=["Questions"],
        summary="Partially update a question (also appends an answer if attributes.answer provided)",
    ),
    destroy=extend_schema(tags=["Questions"], summary="Delete a question"),
)
class QuestionViewSet(BaseViewSet):
    model = Question
    serializer_class = QuestionSerializer



    @extend_schema(
        tags=["Questions"],
        summary="List questions (auto-includes company)",
        parameters=_PAGE_PARAMS,
        responses={200: _JSONAPI_LIST},
    )
    def list(self, request):
        qs = Question.objects.filter(created_by_id=request.user.id)

        query_filter = request.query_params.get("filter[query]")
        if query_filter:
            qs = qs.filter(content__icontains=query_filter)

        job_post_filter = request.query_params.get("filter[job_post_id]")
        if job_post_filter:
            qs = qs.filter(job_post_id=job_post_filter)

        application_filter = request.query_params.get("filter[application_id]")
        if application_filter:
            qs = qs.filter(application_id=application_filter)

        qs = qs.order_by("-id")
        total = qs.count()
        page_number, page_size = self._page_params()
        total_pages = math.ceil(total / page_size) if page_size else 1
        offset = (page_number - 1) * page_size

        include_rels = self._parse_include(request) or ["company"]
        # Prefetch answers in-bulk to avoid N+1 when include=answers is requested
        if "answers" in include_rels or "answer" in include_rels:
            qs = qs.prefetch_related("answers")

        items = list(qs.all()[offset: offset + page_size])

        ser = self.get_serializer()
        payload = {
            "data": [ser.to_resource(o) for o in items],
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

        if include_rels:
            payload["included"] = self._build_included(items, include_rels, request)
        return Response(payload)

    @extend_schema(
        tags=["Questions"],
        summary="Retrieve a question (auto-includes company)",
        parameters=[_INCLUDE_PARAM],
        responses={200: _JSONAPI_ITEM, 404: OpenApiResponse(description="Not found")},
    )
    def retrieve(self, request, pk=None):
        include_rels = self._parse_include(request) or ["company"]
        qs = Question.objects
        if "answers" in include_rels or "answer" in include_rels:
            qs = qs.prefetch_related("answers")
        obj = qs.filter(pk=pk).first()
        if not obj or obj.created_by_id != request.user.id:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer()
        payload = {"data": ser.to_resource(obj)}
        if include_rels:
            payload["included"] = self._build_included([obj], include_rels, request)
        return Response(payload)

    @extend_schema(
        tags=["Questions"],
        summary="Create a question (optionally include attributes.answer to auto-create an Answer child)",
        request=_JSONAPI_WRITE,
        responses={
            201: _JSONAPI_ITEM,
            400: OpenApiResponse(description="Validation error"),
        },
    )
    def create(self, request):
        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
            attrs["created_by_id"] = request.user.id
        except ValueError as e:
            return Response({"errors": [{"detail": str(e)}]}, status=400)

        attrs = self.pre_save_payload(request, attrs, creating=True)
        # Backfill company_id and job_post_id from the application if not supplied
        if attrs.get("application_id") and (
            not attrs.get("company_id") or not attrs.get("job_post_id")
        ):
            app = JobApplication.objects.filter(pk=attrs["application_id"]).first()
            if app:
                attrs.setdefault("company_id", app.company_id)
                attrs.setdefault("job_post_id", app.job_post_id)
        # Remove SA-incompatible attrs; keep only model field names
        safe_attrs = {
            k: v
            for k, v in attrs.items()
            if k
            in ("content", "application_id", "company_id", "created_by_id", "job_post_id")
        }
        obj = Question.objects.create(**safe_attrs)

        # Back-compat write path: accept attributes.answer and create a child Answer
        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") or {}
        attrs_node = node.get("attributes") or {}
        ans_val = attrs_node.get("answer")
        ans_str = ans_val.strip() if isinstance(ans_val, str) else None
        if ans_str:
            try:
                Answer.objects.create(question_id=obj.id, content=ans_str)
            except Exception:
                pass

        payload = {"data": ser.to_resource(obj)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([obj], include_rels, request)
        return Response(payload, status=status.HTTP_201_CREATED)

    def _upsert(self, request, pk, partial=False):
        obj = Question.objects.filter(pk=pk).first()
        if not obj or obj.created_by_id != request.user.id:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response({"errors": [{"detail": str(e)}]}, status=400)
        attrs = self.pre_save_payload(request, attrs, creating=False)
        for k, v in attrs.items():
            if k in (
                "content",
                "application_id",
                "company_id",
                "created_by_id",
            ):
                setattr(obj, k, v)
        obj.save()

        # Back-compat write path on update: append a new child Answer if provided
        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") or {}
        attrs_node = node.get("attributes") or {}
        ans_val = attrs_node.get("answer")
        ans_str = ans_val.strip() if isinstance(ans_val, str) else None
        if ans_str:
            try:
                Answer.objects.create(question_id=obj.id, content=ans_str)
            except Exception:
                pass

        payload = {"data": ser.to_resource(obj)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([obj], include_rels, request)
        return Response(payload)

    def destroy(self, request, pk=None):
        obj = Question.objects.filter(pk=pk).first()
        if not obj or obj.created_by_id != request.user.id:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        tags=["Questions"],
        summary="List answers for a question",
        responses={200: _JSONAPI_LIST},
    )
    @action(detail=True, methods=["get"])
    def answers(self, request, pk=None):
        obj = Question.objects.filter(pk=pk).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        items = list(Answer.objects.filter(question_id=obj.id).order_by("created_at"))
        ser = AnswerSerializer()
        data = [ser.to_resource(i) for i in items]

        include_rels = self._parse_include(request)
        payload = {"data": data}
        if include_rels:
            payload["included"] = self._build_included(
                items, include_rels, request, primary_serializer=ser
            )
        return Response(payload)


@extend_schema_view(
    list=extend_schema(tags=["Answers"], summary="List answers"),
    retrieve=extend_schema(tags=["Answers"], summary="Retrieve an answer"),
    update=extend_schema(tags=["Answers"], summary="Update an answer"),
    partial_update=extend_schema(
        tags=["Answers"], summary="Partially update an answer"
    ),
    destroy=extend_schema(tags=["Answers"], summary="Delete an answer"),
)
class AnswerViewSet(BaseViewSet):
    model = Answer
    serializer_class = AnswerSerializer

    @extend_schema(
        tags=["Answers"],
        summary="List answers",
        parameters=_PAGE_PARAMS,
        responses={200: _JSONAPI_LIST},
    )
    def list(self, request):
        qs = Answer.objects.filter(question__created_by_id=request.user.id)

        query_filter = request.query_params.get("filter[query]")
        if query_filter:
            qs = qs.filter(
                Q(content__icontains=query_filter) |
                Q(question__content__icontains=query_filter)
            ).distinct()

        question_filter = request.query_params.get("filter[question_id]")
        if question_filter:
            qs = qs.filter(question_id=question_filter)

        qs = qs.order_by("-id")
        total = qs.count()
        page_number, page_size = self._page_params()
        total_pages = math.ceil(total / page_size) if page_size else 1
        offset = (page_number - 1) * page_size
        items = list(qs.all()[offset: offset + page_size])

        ser = self.get_serializer()
        payload = {
            "data": [ser.to_resource(o) for o in items],
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

        include_rels = self._parse_include(request) or ["question"]
        if include_rels:
            payload["included"] = self._build_included(items, include_rels, request)
        return Response(payload)

    @extend_schema(
        tags=["Answers"],
        summary="Create an answer (set ai_assist=true to auto-generate content via AI)",
        request=inline_serializer(
            name="AnswerCreateRequest",
            fields={
                "question_id": drf_serializers.IntegerField(
                    help_text="Required. ID of the parent Question."
                ),
                "content": drf_serializers.CharField(
                    required=False,
                    help_text="Answer text. Required unless ai_assist=true.",
                ),
                "ai_assist": drf_serializers.BooleanField(
                    required=False,
                    help_text="If true and content is empty, AI generates the answer.",
                ),
                "injected_prompt": drf_serializers.CharField(
                    required=False,
                    help_text="Optional custom prompt injected into AI generation.",
                ),
            },
        ),
        responses={
            201: _JSONAPI_ITEM,
            202: OpenApiResponse(description="AI generation started — poll the returned resource for state changes"),
            400: OpenApiResponse(description="Missing content or invalid question"),
            503: OpenApiResponse(description="AI client not configured"),
        },
    )
    def create(self, request):
        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response({"errors": [{"detail": str(e)}]}, status=400)

        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") or {}
        attrs_node = node.get("attributes") or {}
        relationships = node.get("relationships") or {}

        def _first_id(n):
            if isinstance(n, dict):
                d = n.get("data", n)
                if isinstance(d, dict) and "id" in d:
                    return d.get("id")
            return None

        # Resolve question_id from attrs, relationships, or convenience keys
        qid = attrs.get("question_id")
        if qid is None:
            qid = _first_id(
                relationships.get("question") or relationships.get("questions")
            )
        if qid is None:
            qid = (
                attrs_node.get("question_id")
                or attrs_node.get("questionId")
                or attrs_node.get("question-id")
                or node.get("question_id")
                or node.get("questionId")
                or node.get("question-id")
                or data.get("question_id")
                or data.get("questionId")
                or data.get("question-id")
            )
        try:
            qid = int(qid) if qid is not None else None
        except (TypeError, ValueError):
            return Response({"errors": [{"detail": "Invalid question ID"}]}, status=400)

        question = Question.objects.filter(pk=qid).first() if qid is not None else None
        if question is None:
            return Response(
                {"errors": [{"detail": "Missing or invalid question relationship"}]},
                status=400,
            )

        # Determine content, ai_assist flag, and injected_prompt
        content = attrs.get("content")
        if isinstance(content, str):
            content = content.strip()

        ai_flag_raw = (
            attrs_node.get("ai_assist")
            or node.get("ai_assist")
            or data.get("ai_assist")
            or attrs.get("ai_assist")
        )

        # Extract injected prompt for AI assistance
        injected_prompt = (
            attrs_node.get("injected_prompt")
            or attrs_node.get("prompt")
            or node.get("injected_prompt")
            or node.get("prompt")
            or data.get("injected_prompt")
            or data.get("prompt")
            or attrs.get("injected_prompt")
            or attrs.get("prompt")
        )
        if isinstance(injected_prompt, str):
            injected_prompt = injected_prompt.strip()
        else:
            injected_prompt = None

        def _to_bool(v):
            if isinstance(v, bool):
                return v
            if v is None:
                return False
            s = str(v).strip().lower()
            return s in ("1", "true", "yes", "y", "on")

        ai_assist = _to_bool(ai_flag_raw)

        # Resolve optional resume_id — 0 or absent means use career-data
        resume_id_raw = (
            attrs.get("resume_id")
            or _first_id(relationships.get("resume") or relationships.get("resumes"))
            or attrs_node.get("resume_id") or attrs_node.get("resumeId")
            or node.get("resume_id") or data.get("resume_id")
        )
        try:
            resume_id_int = int(resume_id_raw) if resume_id_raw is not None else 0
        except (TypeError, ValueError):
            resume_id_int = 0

        answer_career_markdown = None
        if resume_id_int:
            answer_resume = Resume.objects.filter(pk=resume_id_int).first()
            if not answer_resume:
                return Response({"errors": [{"detail": "Resume not found"}]}, status=400)
            answer_career_markdown = DbExportService().resume_markdown_export(answer_resume)
        else:
            career_data = CareerData.for_user(request.user.id)
            prompt_builder = ApplicationPromptBuilder(max_section_chars=60000)
            answer_career_markdown = prompt_builder.build_from_career_data(career_data) or None

        if not content and ai_assist:
            # AI generation — create a pending record and dispatch async
            client = get_client(required=False)
            if client is None:
                return Response(
                    {"errors": [{"detail": "AI client not configured. Set OPENAI_API_KEY."}]},
                    status=503,
                )

            obj = Answer.objects.create(question_id=question.id, status="pending")
            ans_id = obj.id
            captured_prompt = injected_prompt
            captured_career_markdown = answer_career_markdown

            def _generate():
                import django
                django.db.close_old_connections()
                logger.info("AnswerViewSet._generate: start ans_id=%s question_id=%s", ans_id, question.id)
                try:
                    svc = AnswerService(client)
                    logger.info("AnswerViewSet._generate: calling generate_answer ans_id=%s injected_prompt=%r", ans_id, captured_prompt)
                    result = svc.generate_answer(
                        question=question,
                        save=False,
                        injected_prompt=captured_prompt,
                        career_markdown=captured_career_markdown,
                    )
                    logger.info("AnswerViewSet._generate: got result type=%s ans_id=%s", type(result).__name__, ans_id)
                    generated_content = result.content if isinstance(result, Answer) else str(result or "")
                    logger.info("AnswerViewSet._generate: saving content len=%s ans_id=%s", len(generated_content) if generated_content else 0, ans_id)
                    Answer.objects.filter(pk=ans_id).update(
                        content=generated_content, status="completed"
                    )
                    logger.info("AnswerViewSet._generate: completed ans_id=%s", ans_id)
                except Exception:
                    logger.exception("AnswerViewSet._generate: failed ans_id=%s", ans_id)
                    Answer.objects.filter(pk=ans_id).update(status="failed")

            import threading
            threading.Thread(target=_generate, daemon=True).start()

            return Response({"data": ser.to_resource(obj)}, status=status.HTTP_202_ACCEPTED)

        # Synchronous path — content provided directly
        if not content:
            return Response(
                {
                    "errors": [
                        {
                            "detail": "content is required when ai_assist is not true"
                        }
                    ]
                },
                status=400,
            )
        obj = Answer.objects.create(question_id=question.id, content=content)

        payload = {"data": ser.to_resource(obj)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([obj], include_rels, request)
        return Response(payload, status=status.HTTP_201_CREATED)

    def retrieve(self, request, pk=None):
        obj = Answer.objects.filter(pk=pk).select_related("question").first()
        if not obj or obj.question.created_by_id != request.user.id:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer()
        return Response({"data": ser.to_resource(obj)})

    def _upsert(self, request, pk, partial=False):
        obj = Answer.objects.filter(pk=pk).select_related("question").first()
        if not obj or obj.question.created_by_id != request.user.id:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response({"errors": [{"detail": str(e)}]}, status=400)
        for k, v in attrs.items():
            if k in ("content", "favorite", "status"):
                setattr(obj, k, v)
        obj.save()
        self._sync_question_favorite(obj.question_id)
        return Response({"data": ser.to_resource(obj)})

    def update(self, request, pk=None):
        return self._upsert(request, pk)

    def partial_update(self, request, pk=None):
        return self._upsert(request, pk, partial=True)

    def destroy(self, request, pk=None):
        obj = Answer.objects.filter(pk=pk).select_related("question").first()
        if not obj or obj.question.created_by_id != request.user.id:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        question_id = obj.question_id
        obj.delete()
        self._sync_question_favorite(question_id)
        return Response(status=204)

    @staticmethod
    def _sync_question_favorite(question_id):
        has_fav = Answer.objects.filter(question_id=question_id, favorite=True).exists()
        Question.objects.filter(pk=question_id).update(favorite=has_fav)

