import math

from django.db.models import Max, Q
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from drf_spectacular.utils import (
    extend_schema,
    extend_schema_view,
)

from .base import BaseViewSet
from ._schema import _JSONAPI_LIST
from ..serializers import (
    CompanySerializer,
    JobPostSerializer,
    JobApplicationSerializer,
    ScrapeSerializer,
    ScoreSerializer,
    QuestionSerializer,
)
from job_hunting.models import (
    Company,
    JobPost,
    JobApplication,
    Score,
    Scrape,
    Question,
)


@extend_schema_view(
    list=extend_schema(tags=["Companies"], summary="List companies"),
    retrieve=extend_schema(tags=["Companies"], summary="Retrieve a company"),
    create=extend_schema(tags=["Companies"], summary="Create a company"),
    update=extend_schema(tags=["Companies"], summary="Update a company"),
    partial_update=extend_schema(
        tags=["Companies"], summary="Partially update a company"
    ),
    destroy=extend_schema(tags=["Companies"], summary="Delete a company"),
)
class CompanyViewSet(BaseViewSet):
    model = Company
    serializer_class = CompanySerializer

    def create(self, request):
        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response({"errors": [{"detail": str(e)}]}, status=400)
        name = attrs.get("name", "").strip()
        if not name:
            return Response({"errors": [{"detail": "Company name is required."}]}, status=400)
        existing = Company.objects.filter(name__iexact=name).first()
        if existing:
            return Response({"data": ser.to_resource(existing)}, status=200)
        attrs["name"] = name
        obj = Company.objects.create(**attrs)
        return Response({"data": ser.to_resource(obj)}, status=201)

    def list(self, request):
        qs = Company.objects

        query_filter = request.query_params.get("filter[query]")
        if query_filter is not None:
            qs = qs.filter(
                Q(name__icontains=query_filter) | Q(display_name__icontains=query_filter)
            ).distinct()

        sort_param = request.query_params.get("sort", "relevant")
        if sort_param in ("relevant", "-relevant"):
            qs = qs.annotate(
                latest_job_post=Max(
                    "job_posts__created_at",
                    filter=Q(job_posts__created_by_id=request.user.id),
                )
            ).order_by("-latest_job_post")
        elif sort_param:
            sort_fields = []
            for field in sort_param.split(","):
                field = field.strip()
                if field.startswith("-"):
                    sort_fields.append(f"-{field[1:]}")
                else:
                    sort_fields.append(field)
            if sort_fields:
                qs = qs.order_by(*sort_fields)

        total = qs.count()
        page_number, page_size = self._page_params()
        total_pages = math.ceil(total / page_size) if page_size else 1
        offset = (page_number - 1) * page_size
        items = list(qs.all()[offset: offset + page_size])

        ser = self.get_serializer()
        payload = {
            "data": [ser.to_resource(obj) for obj in items],
            "meta": {
                "total": total,
                "page": page_number,
                "per_page": page_size,
                "total_pages": total_pages,
            },
        }
        if page_number < total_pages:
            base = request.build_absolute_uri(request.path)
            qp = request.query_params.dict()
            qp["page"] = page_number + 1
            qp["per_page"] = page_size
            payload["links"] = {"next": base + "?" + "&".join(f"{k}={v}" for k, v in qp.items())}
        else:
            payload["links"] = {"next": None}

        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included(items, include_rels, request)
        return Response(payload)

    def retrieve(self, request, pk=None):
        obj = Company.objects.filter(pk=pk).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer()
        resource = ser.to_resource(obj)
        resource["meta"] = {
            "job_posts_count": obj.job_posts.count(),
            "job_applications_count": JobApplication.objects.filter(
                job_post__company_id=obj.id
            ).count(),
            "scrapes_count": obj.scrapes.count(),
            "questions_count": obj.questions.count(),
            "scores_count": Score.objects.filter(
                job_post__company_id=obj.id
            ).count(),
        }
        payload = {"data": resource}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([obj], include_rels, request)
        return Response(payload)

    def update(self, request, pk=None):
        obj = Company.objects.filter(pk=pk).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = request.data.get("data", {})
        attrs = data.get("attributes", {})
        for field in ("name", "display_name", "notes"):
            if field in attrs:
                setattr(obj, field, attrs[field])
        obj.save()
        ser = self.get_serializer()
        return Response({"data": ser.to_resource(obj)})

    def partial_update(self, request, pk=None):
        return self.update(request, pk=pk)

    def destroy(self, request, pk=None):
        if not request.user.is_staff:
            return Response(
                {"errors": [{"detail": "Only staff may delete companies."}]},
                status=status.HTTP_403_FORBIDDEN,
            )
        obj = Company.objects.filter(pk=pk).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(
        tags=["Companies"],
        summary="List job posts for a company",
        responses={200: _JSONAPI_LIST},
    )
    @action(detail=True, methods=["get"], url_path="job-posts")
    def job_posts(self, request, pk=None):
        if not Company.objects.filter(pk=pk).exists():
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        posts = list(
            JobPost.objects.filter(company_id=int(pk)).filter(
                Q(created_by_id=request.user.id) |
                Q(applications__user_id=request.user.id) |
                Q(scores__user_id=request.user.id)
            ).distinct()
        )
        data = [JobPostSerializer().to_resource(j) for j in posts]
        return Response({"data": data})

    @extend_schema(
        tags=["Companies"],
        summary="List job applications for a company",
        responses={200: _JSONAPI_LIST},
    )
    @action(detail=True, methods=["get"], url_path="job-applications")
    def applications(self, request, pk=None):
        if not Company.objects.filter(pk=pk).exists():
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        apps = list(JobApplication.objects.filter(company_id=int(pk), user_id=request.user.id))
        data = [JobApplicationSerializer().to_resource(a) for a in apps]
        return Response({"data": data})

    @extend_schema(
        tags=["Companies"],
        summary="List scrapes for a company",
        responses={200: _JSONAPI_LIST},
    )
    @action(detail=True, methods=["get"])
    def scrapes(self, request, pk=None):
        if not Company.objects.filter(pk=pk).exists():
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        scrapes_list = list(Scrape.objects.filter(company_id=int(pk)))
        data = [ScrapeSerializer().to_resource(s) for s in scrapes_list]
        return Response({"data": data})

    @extend_schema(
        tags=["Companies"],
        summary="List scores for a company",
        responses={200: _JSONAPI_LIST},
    )
    @action(detail=True, methods=["get"])
    def scores(self, request, pk=None):
        if not Company.objects.filter(pk=pk).exists():
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        scores_list = list(
            Score.objects.filter(
                job_post__company_id=int(pk), user_id=request.user.id
            )
        )
        data = [ScoreSerializer().to_resource(s) for s in scores_list]
        return Response({"data": data})

    @extend_schema(
        tags=["Companies"],
        summary="List questions for a company",
        responses={200: _JSONAPI_LIST},
    )
    @action(detail=True, methods=["get"])
    def questions(self, request, pk=None):
        if not Company.objects.filter(pk=pk).exists():
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        questions_list = list(
            Question.objects.filter(company_id=int(pk))
        )
        data = [QuestionSerializer().to_resource(q) for q in questions_list]
        return Response({"data": data})

