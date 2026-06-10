import math

from django.db import transaction
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
    CompanyAlias,
    DuplicateAnnotation,
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
        # JobPost is universal; per-user visibility flows through the same
        # set of signals as JobPostViewSet.list. Without `discoveries` here
        # email-ingested posts were invisible on the company page even
        # though the user had been notified via cc_auto. Staff bypass
        # mirrors list() — every post on the company shows.
        if request.user.is_staff:
            qs = JobPost.objects.filter(company_id=int(pk))
        else:
            qs = JobPost.objects.filter(company_id=int(pk)).filter(
                Q(created_by_id=request.user.id) |
                Q(applications__user_id=request.user.id) |
                Q(scores__user_id=request.user.id) |
                Q(scrapes__created_by_id=request.user.id) |
                Q(discoveries__user_id=request.user.id)
            ).distinct()
        posts = list(qs)
        # Pre-attach user-scoped `_top_score` to each JobPost so the
        # serializer reports the requesting user's highest score, not
        # the cross-user max. Without this the model property falls
        # through to the unscoped query and leaks other users' scores
        # via this shared JobPost row (the same leak the serializer's
        # null-emit guard catches when `_top_score` is absent).
        if posts:
            post_ids = [j.id for j in posts]
            top_score_map = {}
            for s in Score.objects.filter(
                job_post_id__in=post_ids, user_id=request.user.id,
            ).order_by("job_post_id", "-score"):
                top_score_map.setdefault(s.job_post_id, s)
            for j in posts:
                j._top_score = top_score_map.get(j.id)
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
        # Scope to the requesting user's own scrapes. Staff bypasses the
        # filter (admin tools surface every scrape on the company; mirrors
        # the staff bypass on `.job_posts`). Without this filter the
        # endpoint leaked every other user's scrape rows for the shared
        # Company — same tenancy boundary as `Score`, `JobApplication`,
        # `CoverLetter`.
        qs = Scrape.objects.filter(company_id=int(pk))
        if not request.user.is_staff:
            qs = qs.filter(created_by_id=request.user.id)
        scrapes_list = list(qs)
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

    @extend_schema(
        tags=["Companies"],
        summary="Merge this company into another (staff only)",
        responses={
            200: _JSONAPI_LIST,
            400: None,
            403: None,
            404: None,
        },
    )
    @action(detail=True, methods=["post"], url_path="merge-into")
    def merge_into(self, request, pk=None):
        """Move all FKs from this Company into ``target_id`` and delete source.

        Phase A of the dedupe redesign. Staff-only — destructive
        operation that consolidates duplicate Company rows. The
        sequence is wrapped in a single atomic block so the database
        either reaches the target-only state or remains untouched.

        Body shape (plain JSON or JSON:API both accepted):

            {"target_id": <int>}
            {"data": {"attributes": {"target_id": <int>}}}

        Side effects:
        - ``JobPost.company_id`` and ``Scrape.company_id`` and
          ``JobApplication.company_id`` rows pointing at ``pk`` are
          repointed at ``target_id``.
        - ``CompanyAlias.company_id`` rows are repointed. Unique
          collisions on ``name_slug`` are dropped (the target's
          version wins; the source's row is deleted).
        - The source Company is deleted.
        - One ``DuplicateAnnotation`` row is written with
          ``action="company_merge"``; ``signal_state`` captures
          source + target + counts. When the source Company has at
          least one moved JobPost, that JP anchors the annotation's
          ``from_jp`` FK; when there were zero moved JPs, no
          annotation row is written (the operation is still logged).
        """
        if not request.user.is_staff:
            return Response(
                {"errors": [{"detail": "Only staff may merge companies."}]},
                status=status.HTTP_403_FORBIDDEN,
            )

        source = Company.objects.filter(pk=pk).first()
        if not source:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        # Body parse: accept both plain JSON and JSON:API shapes.
        target_id = self._extract_target_id(request.data)
        if target_id is None:
            return Response(
                {"errors": [{"detail": "target_id is required."}]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            target_id = int(target_id)
        except (TypeError, ValueError):
            return Response(
                {"errors": [{"detail": "target_id must be an integer."}]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if target_id == source.id:
            return Response(
                {"errors": [{"detail": "Cannot merge a company into itself."}]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        target = Company.objects.filter(pk=target_id).first()
        if not target:
            return Response(
                {"errors": [{"detail": "target Company not found."}]},
                status=status.HTTP_404_NOT_FOUND,
            )

        with transaction.atomic():
            # Move JobPost FKs first so we can pick an anchor for the
            # audit annotation BEFORE the source is deleted.
            moved_jobposts = list(
                JobPost.objects.filter(company_id=source.id).values_list("id", flat=True)
            )
            JobPost.objects.filter(company_id=source.id).update(company_id=target.id)

            # Move Scrape + JobApplication FKs the same way — staff
            # would otherwise have orphans pointing at a deleted
            # Company once we delete the source.
            Scrape.objects.filter(company_id=source.id).update(company_id=target.id)
            JobApplication.objects.filter(company_id=source.id).update(
                company_id=target.id
            )

            # Move alias rows. Any source alias whose name_slug
            # collides with an existing alias on the target (because
            # both Companies carried the same slug variant) is dropped
            # — the target's row remains. Without the collision check
            # the UPDATE would hit the global UNIQUE on name_slug and
            # 500 the whole merge.
            target_alias_slugs = set(
                CompanyAlias.objects.filter(company_id=target.id)
                .values_list("name_slug", flat=True)
            )
            source_aliases = list(CompanyAlias.objects.filter(company_id=source.id))
            moved_alias_count = 0
            for alias in source_aliases:
                if alias.name_slug in target_alias_slugs:
                    alias.delete()
                else:
                    alias.company_id = target.id
                    alias.save(update_fields=["company"])
                    moved_alias_count += 1

            signal_state = {
                "source_company_id": source.id,
                "source_company_name": source.name,
                "target_company_id": target.id,
                "moved_jobpost_count": len(moved_jobposts),
                "moved_alias_count": moved_alias_count,
            }

            # Audit annotation. ``from_jp`` is non-nullable on the
            # model so we anchor on the first moved JP when one exists.
            # Zero-JP merges (Company minted by a typo-only path) skip
            # the annotation row — the merge is still observable via
            # the deletion + the response payload.
            if moved_jobposts:
                anchor_jp_id = moved_jobposts[0]
                DuplicateAnnotation.objects.create(
                    from_jp_id=anchor_jp_id,
                    to_jp_id=anchor_jp_id,
                    previous_to=None,
                    action="company_merge",
                    set_by=request.user if request.user.is_authenticated else None,
                    signal_state=signal_state,
                )

            source.delete()

        ser = self.get_serializer()
        return Response({"data": ser.to_resource(target)}, status=status.HTTP_200_OK)

    @staticmethod
    def _extract_target_id(payload):
        """Pull ``target_id`` from plain-JSON or JSON:API body shapes."""
        if not isinstance(payload, dict):
            return None
        if "target_id" in payload:
            return payload["target_id"]
        data = payload.get("data")
        if isinstance(data, dict):
            attrs = data.get("attributes") or {}
            if isinstance(attrs, dict) and "target_id" in attrs:
                return attrs["target_id"]
        return None

