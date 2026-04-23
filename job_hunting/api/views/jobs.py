import math

from datetime import timedelta

from django.db.models import Q, F, OuterRef, Subquery
from django.db.models.functions import Length
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from drf_spectacular.utils import (
    extend_schema,
    extend_schema_view,
    OpenApiResponse,
)

from .base import BaseViewSet
from ._schema import (
    _PAGE_PARAMS,
    _SORT_PARAM,
    _FILTER_QUERY_PARAM,
    _FILTER_COMPANY_PARAM,
    _FILTER_COMPANY_ID_PARAM,
    _FILTER_TITLE_PARAM,
    _FILTER_APP_QUERY_PARAM,
    _FILTER_APP_STATUS_PARAM,
    _JSONAPI_LIST,
    _JSONAPI_ITEM,
    _JSONAPI_WRITE,
)
from ..serializers import (
    JobPostSerializer,
    ScoreSerializer,
    ScrapeSerializer,
    CoverLetterSerializer,
    JobApplicationSerializer,
    SummarySerializer,
    QuestionSerializer,
    StatusSerializer,
    JobApplicationStatusSerializer,
)
from job_hunting.api.permissions import IsGuestReadOnly
from job_hunting.lib.ai_client import get_client
from job_hunting.lib.services.summary_service import SummaryService
from job_hunting.models import (
    Status,
    Summary,
    JobPost,
    JobApplication,
    CoverLetter,
    Resume,
    Score,
    Scrape,
    Question,
    JobApplicationStatus,
    ResumeSummary,
)


def _attach_active_application_status(job_posts, user_id):
    """Pre-attach `_active_application_status` (string or None) on each
    JobPost: the latest JobApplicationStatus.status name on the user's own
    application for that post. One query for the whole batch."""
    if not job_posts or not user_id:
        for jp in job_posts:
            jp._active_application_status = None
        return
    post_ids = [jp.id for jp in job_posts]
    from job_hunting.models import JobApplicationStatus
    rows = (
        JobApplicationStatus.objects
        .filter(application__job_post_id__in=post_ids, application__user_id=user_id)
        .select_related("status", "application")
        .order_by("application__job_post_id", "-logged_at", "-created_at")
    )
    status_map = {}
    for jas in rows:
        pid = jas.application.job_post_id
        if pid in status_map:
            continue
        status_map[pid] = jas.status.status if jas.status_id else None
    for jp in job_posts:
        jp._active_application_status = status_map.get(jp.id)


@extend_schema_view(
    list=extend_schema(
        tags=["Job Posts"],
        summary="List job posts",
        parameters=_PAGE_PARAMS + [
            _SORT_PARAM,
            _FILTER_QUERY_PARAM,
            _FILTER_COMPANY_PARAM,
            _FILTER_COMPANY_ID_PARAM,
            _FILTER_TITLE_PARAM,
        ],
    ),
    retrieve=extend_schema(tags=["Job Posts"], summary="Retrieve a job post"),
    create=extend_schema(
        tags=["Job Posts"],
        summary="Create a job post (created_by set to authenticated user)",
    ),
    update=extend_schema(tags=["Job Posts"], summary="Update a job post"),
    partial_update=extend_schema(
        tags=["Job Posts"], summary="Partially update a job post"
    ),
    destroy=extend_schema(tags=["Job Posts"], summary="Delete a job post"),
)
class JobPostViewSet(BaseViewSet):
    model = JobPost
    serializer_class = JobPostSerializer



    def pre_save_payload(self, request, attrs, creating):
        # Remove any client-supplied ownership fields so they can't be spoofed
        attrs.pop("created_by", None)
        attrs.pop("created_by_id", None)  # defensive

        # If creating, set created_by_id to the authenticated user
        if creating:
            attrs["created_by_id"] = request.user.id

        return attrs

    @staticmethod
    def _parse_date_attrs(attrs):
        """Parse posted_date and extraction_date from ISO strings to date objects."""
        from dateutil import parser as dateutil_parser
        from datetime import date as date_type

        errors = {}
        for field in ("posted_date", "extraction_date"):
            if field not in attrs or attrs[field] is None:
                continue
            val = attrs[field]
            if isinstance(val, date_type):
                continue
            try:
                attrs[field] = dateutil_parser.parse(str(val)).date()
            except (ValueError, TypeError):
                errors[field] = (
                    f"Invalid {field}: {val!r}. Expected a date (e.g. '2025-01-15')."
                )
        return errors

    def list(self, request):
        qs = JobPost.objects.filter(
            Q(created_by_id=request.user.id) |
            Q(applications__user_id=request.user.id) |
            Q(scores__user_id=request.user.id) |
            Q(scrapes__created_by_id=request.user.id)
        ).distinct()
        link_filter = request.query_params.get("filter[link]")
        if link_filter is not None:
            qs = qs.filter(link=link_filter)

        hostname_filter = request.query_params.get("filter[hostname]")
        if hostname_filter is not None:
            if hostname_filter == "(direct)":
                qs = qs.filter(Q(link__isnull=True) | Q(link=""))
            else:
                qs = qs.filter(link__icontains=hostname_filter)

        scored_filter = request.query_params.get("filter[scored]")
        if scored_filter is not None:
            wants_scored = str(scored_filter).lower() in ("1", "true", "yes")
            scored_ids = qs.filter(scores__user_id=request.user.id).values("id")
            if wants_scored:
                qs = qs.filter(id__in=scored_ids)
            else:
                qs = qs.exclude(id__in=scored_ids)

        source_filter = request.query_params.get("filter[source]")
        if source_filter is not None and source_filter != "all":
            qs = qs.filter(source=source_filter)

        # Exclude posts the user has manually triaged out. "Vetted Bad"
        # is a pre-application triage label (see application_flow.BUCKETS)
        # the user applies to posts they've decided not to pursue. Excludes
        # when the LATEST status on the user's application is "Vetted Bad"
        # — re-opens if they log a newer status.
        exclude_vetted_bad = request.query_params.get(
            "filter[exclude_vetted_bad]"
        )
        if str(exclude_vetted_bad).lower() in ("1", "true", "yes"):
            latest_status_subq = (
                JobApplicationStatus.objects.filter(
                    application__job_post=OuterRef("pk"),
                    application__user_id=request.user.id,
                )
                .order_by("-logged_at", "-created_at")
                .values("status__status")[:1]
            )
            qs = qs.annotate(
                _vb_latest=Subquery(latest_status_subq)
            ).filter(
                Q(_vb_latest__isnull=True) | ~Q(_vb_latest="Vetted Bad")
            )

        # Sankey bucket filter (applied/interview/offer/ghosted/rejected/
        # withdrew/accepted/declined/no_application). Scopes posts where
        # the caller's application's LATEST status falls in the bucket.
        # Mirrors the mapping in
        # job_hunting.lib.services.application_flow.BUCKETS so clicking a
        # sankey node and landing here keeps the same population.
        bucket_filter = request.query_params.get("filter[bucket]")
        if bucket_filter:
            from job_hunting.lib.services.application_flow import (
                BUCKETS,
                BUCKET_GHOSTED,
                GHOST_AFTER_DAYS,
                STAGE_BUCKETS,
            )

            if bucket_filter == "no_application":
                qs = qs.exclude(applications__user_id=request.user.id)
            else:
                status_names = [
                    name for name, b in BUCKETS.items() if b == bucket_filter
                ]
                latest = (
                    JobApplicationStatus.objects.filter(
                        application__job_post=OuterRef("pk"),
                        application__user_id=request.user.id,
                    )
                    .order_by("-logged_at", "-created_at")
                )
                latest_status = latest.values("status__status")[:1]
                qs = qs.annotate(_latest_status=Subquery(latest_status))

                if bucket_filter == BUCKET_GHOSTED:
                    stage_names = [
                        n for n, b in BUCKETS.items() if b in STAGE_BUCKETS
                    ]
                    latest_time = latest.values("logged_at")[:1]
                    cutoff = timezone.now() - timedelta(days=GHOST_AFTER_DAYS)
                    qs = qs.annotate(
                        _latest_time=Subquery(latest_time)
                    ).filter(
                        _latest_status__in=stage_names,
                        _latest_time__lt=cutoff,
                    )
                elif status_names:
                    qs = qs.filter(_latest_status__in=status_names)
                else:
                    qs = qs.none()

        # Stub = thin/empty description. Sankey's stub bucket uses a
        # word-count threshold (STUB_MIN_WORDS=60); SQL-side we approximate
        # via char length (~450 chars ≈ 60 words at ~7.5 chars/word incl.
        # whitespace). Good enough for a list filter; canonical definition
        # still lives in application_flow._is_thin_description.
        stub_filter = request.query_params.get("filter[stub]")
        if stub_filter is not None:
            wants_stub = str(stub_filter).lower() in ("1", "true", "yes")
            annotated = qs.annotate(_desc_len=Length("description"))
            if wants_stub:
                qs = annotated.filter(
                    Q(description__isnull=True)
                    | Q(description="")
                    | Q(_desc_len__lt=450)
                )
            else:
                qs = annotated.filter(
                    description__isnull=False, _desc_len__gte=450
                ).exclude(description="")

        company_id_filter = request.query_params.get("filter[company_id]")
        if company_id_filter is not None:
            qs = qs.filter(company_id=company_id_filter)

        company_filter = request.query_params.get("filter[company]")
        if company_filter is not None:
            qs = qs.filter(company__name__icontains=company_filter)

        title_filter = request.query_params.get("filter[title]")
        if title_filter is not None:
            qs = qs.filter(title__icontains=title_filter)

        query_filter = request.query_params.get("filter[query]")
        if query_filter is not None:
            qs = qs.filter(
                Q(title__icontains=query_filter)
                | Q(description__icontains=query_filter)
                | Q(company__name__icontains=query_filter)
                | Q(company__display_name__icontains=query_filter)
            ).distinct()

        sort_param = request.query_params.get("sort")
        if sort_param:
            sort_fields = []
            sort_field_names: set[str] = set()
            for field in sort_param.split(","):
                field = field.strip()
                name = field.lstrip("-")
                sort_field_names.add(name)
                if field.startswith("-"):
                    sort_fields.append(F(name).desc(nulls_last=True))
                else:
                    sort_fields.append(F(name).asc(nulls_last=True))
            # Deterministic tiebreak: fall through to -id when the user's
            # sort could otherwise leave same-day rows in index-random order
            # (e.g. sort=-posted_date with many rows sharing today's date).
            if sort_fields and "id" not in sort_field_names:
                sort_fields.append(F("id").desc())
            if sort_fields:
                qs = qs.order_by(*sort_fields)

        total = qs.count()
        page_number, page_size = self._page_params()
        total_pages = math.ceil(total / page_size) if page_size else 1
        offset = (page_number - 1) * page_size
        items = list(qs.all()[offset: offset + page_size])

        # Attach the highest Score to each job post in one query
        if items:
            job_post_ids = [jp.id for jp in items]
            all_scores = list(
                Score.objects.filter(job_post_id__in=job_post_ids, user_id=request.user.id).order_by("job_post_id", "-score")
            )
            top_score_map = {}
            for s in all_scores:
                if s.job_post_id not in top_score_map:
                    top_score_map[s.job_post_id] = s
            for jp in items:
                jp._top_score = top_score_map.get(jp.id)

            _attach_active_application_status(items, request.user.id)

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
        }
        if page_number < total_pages:
            base = request.build_absolute_uri(request.path)
            # Preserve existing query params, overriding page
            qp = request.query_params.dict()
            qp["page"] = page_number + 1
            qp["per_page"] = page_size
            next_url = base + "?" + "&".join(f"{k}={v}" for k, v in qp.items())
            payload["links"] = {"next": next_url}
        else:
            payload["links"] = {"next": None}

        include_rels = self._parse_include(request) or ["top-score"]
        payload["included"] = self._build_included(items, include_rels, request)
        return Response(payload)

    def retrieve(self, request, pk=None):
        obj = JobPost.objects.filter(pk=pk).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        has_access = (
            obj.created_by_id == request.user.id or
            obj.applications.filter(user_id=request.user.id).exists() or
            obj.scores.filter(user_id=request.user.id).exists() or
            obj.scrapes.filter(created_by_id=request.user.id).exists()
        )
        if not has_access:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        _attach_active_application_status([obj], request.user.id)
        ser = self.get_serializer()
        payload = {"data": ser.to_resource(obj)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([obj], include_rels, request)
        return Response(payload)

    def create(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data) if "data" in data else {}
        except ValueError as e:
            return Response({"errors": [{"detail": str(e)}]}, status=400)
        attrs = self.pre_save_payload(request, attrs, creating=True)
        # Computed read-only properties on the JobPost model — clients echo
        # them back in POST bodies from a prior GET; setattr would raise
        # because they have no setter.
        attrs.pop("top_score", None)
        attrs.pop("active_application_status", None)
        date_errors = self._parse_date_attrs(attrs)
        if date_errors:
            return Response(
                {"errors": [{"detail": v} for v in date_errors.values()]}, status=400
            )
        if attrs.get("link"):
            existing = JobPost.objects.filter(link=attrs["link"]).first()
            if existing:
                return Response({"data": ser.to_resource(existing)}, status=status.HTTP_200_OK)
        obj = JobPost(**attrs)
        obj.save()
        if not obj.posted_date:
            obj.posted_date = obj.created_at.date()
            obj.save(update_fields=["posted_date"])
        return Response({"data": ser.to_resource(obj)}, status=status.HTTP_201_CREATED)

    def update(self, request, pk=None):
        return self._upsert_django(request, pk, partial=False)

    def partial_update(self, request, pk=None):
        return self._upsert_django(request, pk, partial=True)

    def _upsert_django(self, request, pk, partial=False):
        obj = JobPost.objects.filter(pk=pk).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        if obj.created_by_id != request.user.id:
            return Response({"errors": [{"detail": "Forbidden"}]}, status=403)
        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response({"errors": [{"detail": str(e)}]}, status=400)
        attrs = self.pre_save_payload(request, attrs, creating=False)
        attrs.pop("created_by_id", None)
        attrs.pop("created_at", None)  # never allow overriding auto timestamp
        # Computed read-only properties on the JobPost model — clients echo
        # them back in PATCH bodies from the prior GET; setattr would raise
        # because they have no setter.
        attrs.pop("top_score", None)
        attrs.pop("active_application_status", None)
        date_errors = self._parse_date_attrs(attrs)
        if date_errors:
            return Response(
                {"errors": [{"detail": v} for v in date_errors.values()]}, status=400
            )
        for k, v in attrs.items():
            setattr(obj, k, v)
        obj.save()
        if not obj.posted_date:
            obj.posted_date = obj.created_at.date()
            obj.save(update_fields=["posted_date"])
        return Response({"data": ser.to_resource(obj)})

    def destroy(self, request, pk=None):
        obj = JobPost.objects.filter(pk=pk).first()
        if not obj:
            return Response(status=204)
        if not request.user.is_staff and obj.created_by_id != request.user.id:
            return Response({"errors": [{"detail": "Forbidden"}]}, status=403)
        if obj.applications.exclude(user_id=request.user.id).exists():
            return Response(
                {"errors": [{"detail": "Cannot delete: other users have applications on this post"}]},
                status=409,
            )
        obj.delete()
        return Response(status=204)

    @extend_schema(
        tags=["Job Posts"],
        summary="Triage a job post — log a quick status on the user's application",
        request={"application/json": {"type": "object", "properties": {"status": {"type": "string"}}}},
        responses={200: _JSONAPI_ITEM, 400: _JSONAPI_ITEM, 403: _JSONAPI_ITEM, 404: _JSONAPI_ITEM},
    )
    @action(detail=True, methods=["post"], url_path="triage")
    def triage(self, request, pk=None):
        """Log a pre-application triage status (Vetted Good / Vetted Bad) on
        the user's own JobApplication for this post. Creates the application
        on first triage. Also updates the denormalized JobApplication.status
        cache so the app reflects the latest triage everywhere (not just via
        the application_statuses history)."""
        obj = JobPost.objects.filter(pk=pk).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        data = request.data if isinstance(request.data, dict) else {}
        status_name = (data.get("status") or "").strip()
        note = (data.get("note") or "").strip() or None
        allowed = {"Vetted Good", "Vetted Bad"}
        if status_name not in allowed:
            return Response(
                {"errors": [{"detail": f"status must be one of {sorted(allowed)}"}]},
                status=400,
            )

        app = (
            JobApplication.objects.filter(job_post=obj, user=request.user).first()
            or JobApplication.objects.create(job_post=obj, user=request.user)
        )
        status_row = Status.objects.get_or_create(status=status_name)[0]
        from django.utils import timezone as _tz
        from job_hunting.models import JobApplicationStatus
        JobApplicationStatus.objects.create(
            application=app,
            status=status_row,
            logged_at=_tz.now(),
            note=note,
        )
        if app.status != status_name:
            app.status = status_name
            app.save(update_fields=["status"])

        obj._active_application_status = status_name
        ser = self.get_serializer()
        return Response({"data": ser.to_resource(obj)})

    @extend_schema(
        tags=["Job Posts"],
        summary="Queue async re-extraction of job post fields from pasted text",
        request={"application/json": {"type": "object", "properties": {"text": {"type": "string"}}}},
        responses={202: _JSONAPI_ITEM, 400: _JSONAPI_ITEM, 403: _JSONAPI_ITEM, 404: _JSONAPI_ITEM},
    )
    @action(detail=True, methods=["post"], url_path="reextract")
    def reextract(self, request, pk=None):
        """Queue an async re-extraction of pasted text. Creates a Scrape with
        status='pending' linked to this JobPost and hands off to the shared
        parse_scrape pipeline with force=True so the extractor merges fresh
        fields into the existing JobPost (company preserved). Returns the
        Scrape immediately so the client can poll for status transitions."""
        obj = JobPost.objects.filter(pk=pk).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        if obj.created_by_id != request.user.id:
            return Response({"errors": [{"detail": "Forbidden"}]}, status=403)

        data = request.data if isinstance(request.data, dict) else {}
        text = (data.get("text") or "").strip()
        if not text:
            return Response(
                {"errors": [{"detail": "text is required"}]}, status=400
            )

        scrape = Scrape.objects.create(
            url=obj.link or "",
            job_content=text,
            status="pending",
            created_by=request.user,
            job_post=obj,
            source="paste",
        )
        from job_hunting.lib.scraper import _log_scrape_status
        from job_hunting.lib.parsers.job_post_extractor import parse_scrape
        _log_scrape_status(scrape.id, "pending", note="reextract from paste")
        parse_scrape(scrape.id, user_id=request.user.id, force=True)

        scr_ser = ScrapeSerializer()
        return Response(
            {"data": scr_ser.to_resource(scrape)},
            status=status.HTTP_202_ACCEPTED,
        )

    @action(detail=True, methods=["post"], url_path="resolve-apply")
    def resolve_apply(self, request, pk=None):
        """Request apply-URL resolution for this JobPost.

        Phase 1: stub. Enqueues intent by flipping application_status to
        'unknown' (if it was 'stale' or 'failed') so the Phase 2 resolver
        picks it up on its next sweep. Returns the JobPost immediately.

        Ownership-gated. Staff get broader access."""
        obj = JobPost.objects.filter(pk=pk).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        if not request.user.is_staff and obj.created_by_id != request.user.id:
            return Response({"errors": [{"detail": "Forbidden"}]}, status=403)

        if obj.apply_url_status in ("stale", "failed"):
            obj.apply_url_status = "unknown"
            obj.apply_url_resolved_at = None
            obj.save(update_fields=["apply_url_status", "apply_url_resolved_at"])

        jp_ser = self.get_serializer()
        return Response(
            {"data": jp_ser.to_resource(obj)},
            status=status.HTTP_202_ACCEPTED,
        )

    @extend_schema(
        tags=["Job Posts"],
        summary="Nuclear delete — remove job post and ALL child records",
        responses={204: None, 403: _JSONAPI_ITEM},
    )
    @action(detail=True, methods=["delete"], url_path="nuclear")
    def nuclear(self, request, pk=None):
        """Staff-only: delete a job post and every child relation."""
        if not request.user.is_staff:
            return Response(
                {"errors": [{"detail": "Staff only"}]}, status=403
            )
        obj = JobPost.objects.filter(pk=pk).first()
        if not obj:
            return Response(status=204)
        # Delete children that use SET_NULL (won't cascade automatically)
        Question.objects.filter(job_post=obj).delete()
        Score.objects.filter(job_post=obj).delete()
        CoverLetter.objects.filter(job_post=obj).delete()
        Scrape.objects.filter(job_post=obj).delete()
        JobApplication.objects.filter(job_post=obj).delete()
        Summary.objects.filter(job_post_id=obj.pk).delete()
        obj.delete()
        return Response(status=204)

    @extend_schema(
        tags=["Job Posts"],
        summary="List scores for a job post",
        responses={200: _JSONAPI_LIST},
    )
    @action(detail=True, methods=["get"])
    def scores(self, request, pk=None):
        if not JobPost.objects.filter(pk=pk).exists():
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        scores = list(Score.objects.filter(job_post_id=int(pk), user_id=request.user.id))
        data = [ScoreSerializer().to_resource(s) for s in scores]
        return Response({"data": data})

    @extend_schema(
        tags=["Job Posts"],
        summary="List scrapes for a job post",
        responses={200: _JSONAPI_LIST},
    )
    @action(detail=True, methods=["get"])
    def scrapes(self, request, pk=None):
        if not JobPost.objects.filter(pk=pk).exists():
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        scrapes = list(Scrape.objects.filter(job_post_id=int(pk)))
        data = [ScrapeSerializer().to_resource(s) for s in scrapes]
        return Response({"data": data})

    @extend_schema(
        tags=["Job Posts"],
        summary="List cover letters for a job post (authenticated user's only)",
        responses={200: _JSONAPI_LIST},
    )
    @action(
        detail=True,
        methods=["get"],
        url_path="cover-letters",
        permission_classes=[IsAuthenticated],
    )
    def cover_letters(self, request, pk=None):
        if not JobPost.objects.filter(pk=pk).exists():
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        cover_letters = list(
            CoverLetter.objects.filter(job_post_id=int(pk), user_id=request.user.id)
        )
        data = [CoverLetterSerializer().to_resource(c) for c in cover_letters]
        return Response({"data": data})

    @extend_schema(
        tags=["Job Posts"],
        summary="List job applications for a job post",
        responses={200: _JSONAPI_LIST},
    )
    @action(detail=True, methods=["get"], url_path="job-applications")
    def applications(self, request, pk=None):
        if not JobPost.objects.filter(pk=pk).exists():
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        apps = list(JobApplication.objects.filter(job_post_id=int(pk), user_id=request.user.id))
        data = [JobApplicationSerializer().to_resource(a) for a in apps]
        return Response({"data": data})

    @extend_schema(
        methods=["GET"],
        tags=["Job Posts"],
        summary="List questions for a job post",
        responses={200: _JSONAPI_LIST},
    )
    @extend_schema(
        methods=["POST"],
        tags=["Job Posts"],
        summary="Create a question for a job post (company auto-set from job post)",
        request=_JSONAPI_WRITE,
        responses={201: _JSONAPI_ITEM, 404: OpenApiResponse(description="Not found")},
    )
    @action(detail=True, methods=["get", "post"])
    def questions(self, request, pk=None):
        job_post = JobPost.objects.filter(pk=int(pk)).first()
        if not job_post:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        ser = QuestionSerializer()

        if request.method.lower() == "post":
            try:
                attrs = ser.parse_payload(request.data)
            except ValueError as e:
                return Response({"errors": [{"detail": str(e)}]}, status=400)
            attrs["created_by_id"] = request.user.id
            attrs.setdefault("company_id", job_post.company_id)
            attrs.setdefault("job_post_id", job_post.id)
            safe_attrs = {
                k: v
                for k, v in attrs.items()
                if k in ("content", "favorite", "application_id", "company_id", "created_by_id")
            }
            obj = Question.objects.create(**safe_attrs)
            include_rels = self._parse_include(request)
            payload = {"data": ser.to_resource(obj)}
            if include_rels:
                payload["included"] = self._build_included([obj], include_rels, request, primary_serializer=ser)
            return Response(payload, status=status.HTTP_201_CREATED)

        items = list(Question.objects.filter(application__job_post_id=int(pk), created_by_id=request.user.id))
        include_rels = self._parse_include(request)
        payload = {"data": [ser.to_resource(i) for i in items]}
        if include_rels:
            payload["included"] = self._build_included(items, include_rels, request, primary_serializer=ser)
        return Response(payload)

    @extend_schema(
        methods=["GET"],
        tags=["Job Posts"],
        summary="List summaries for a job post",
        responses={200: _JSONAPI_LIST},
    )
    @extend_schema(
        methods=["POST"],
        tags=["Job Posts"],
        summary="Create/AI-generate a summary for a job post",
        request=_JSONAPI_WRITE,
        responses={
            201: _JSONAPI_ITEM,
            400: OpenApiResponse(description="Missing resume"),
            503: OpenApiResponse(description="AI client not configured"),
        },
    )
    @action(detail=True, methods=["get", "post"])
    def summaries(self, request, pk=None):
        if request.method.lower() == "post":
            obj = JobPost.objects.filter(pk=pk).first()
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
                resume = Resume.objects.filter(pk=int(resume_id)).first()
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

            ResumeSummary.objects.filter(resume_id=resume.id).update(active=False)
            ResumeSummary.objects.get_or_create(
                resume_id=resume.id, summary_id=summary.id, defaults={"active": True}
            )
            ResumeSummary.objects.filter(
                resume_id=resume.id, summary_id=summary.id
            ).update(active=True)
            ResumeSummary.ensure_single_active_for_resume(resume.id)

            ser = SummarySerializer()
            payload = {"data": ser.to_resource(summary)}
            include_rels = self._parse_include(request)
            if include_rels:
                payload["included"] = self._build_included(
                    [summary], include_rels, request
                )
            return Response(payload, status=status.HTTP_201_CREATED)

        obj = JobPost.objects.filter(pk=pk).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        summaries = list(Summary.objects.filter(job_post_id=obj.id, user_id=request.user.id))
        data = [SummarySerializer().to_resource(s) for s in summaries]
        return Response({"data": data})


@extend_schema_view(
    list=extend_schema(
        tags=["Job Applications"],
        summary="List job applications",
        parameters=_PAGE_PARAMS + [
            _SORT_PARAM,
            _FILTER_APP_QUERY_PARAM,
            _FILTER_APP_STATUS_PARAM,
            _FILTER_COMPANY_PARAM,
            _FILTER_COMPANY_ID_PARAM,
        ],
    ),
    retrieve=extend_schema(
        tags=["Job Applications"], summary="Retrieve a job application"
    ),
    create=extend_schema(
        tags=["Job Applications"],
        summary="Create a job application (user_id auto-set from authenticated user)",
    ),
    update=extend_schema(tags=["Job Applications"], summary="Update a job application"),
    partial_update=extend_schema(
        tags=["Job Applications"], summary="Partially update a job application"
    ),
    destroy=extend_schema(
        tags=["Job Applications"], summary="Delete a job application"
    ),
)
class JobApplicationViewSet(BaseViewSet):
    model = JobApplication
    serializer_class = JobApplicationSerializer

    def list(self, request):
        qs = JobApplication.objects.filter(user_id=request.user.id)

        company_id_filter = request.query_params.get("filter[company_id]")
        if company_id_filter is not None:
            qs = qs.filter(company_id=company_id_filter)

        company_filter = request.query_params.get("filter[company]")
        if company_filter is not None:
            qs = qs.filter(company__name__icontains=company_filter)

        status_filter = request.query_params.get("filter[status]")
        if status_filter is not None:
            qs = qs.filter(status__icontains=status_filter)

        query_filter = request.query_params.get("filter[query]")
        if query_filter is not None:
            qs = qs.filter(
                Q(job_post__title__icontains=query_filter)
                | Q(company__name__icontains=query_filter)
                | Q(company__display_name__icontains=query_filter)
                | Q(status__icontains=query_filter)
                | Q(notes__icontains=query_filter)
            ).distinct()

        # Handle sorting
        sort_param = request.query_params.get("sort")
        if sort_param:
            sort_fields = []
            sort_field_names: set[str] = set()
            for field in sort_param.split(","):
                field = field.strip()
                name = field.lstrip("-")
                sort_field_names.add(name)
                if field.startswith("-"):
                    sort_fields.append(F(name).desc(nulls_last=True))
                else:
                    sort_fields.append(F(name).asc(nulls_last=True))
            # Deterministic tiebreak: fall through to -id when the user's
            # sort could otherwise leave same-day rows in index-random order
            # (e.g. sort=-posted_date with many rows sharing today's date).
            if sort_fields and "id" not in sort_field_names:
                sort_fields.append(F("id").desc())
            if sort_fields:
                qs = qs.order_by(*sort_fields)

        items = list(qs.all())
        items = self.paginate(items)
        ser = self.get_serializer()
        data = [ser.to_resource(o) for o in items]
        payload = {"data": data}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included(items, include_rels, request)
        return Response(payload)

    def retrieve(self, request, pk=None):
        obj = self._get_obj(pk)
        if not obj or obj.user_id != request.user.id:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer()
        payload = {"data": ser.to_resource(obj)}
        # Always include statuses; merge with any additional ?include= rels
        include_rels = list({*self._parse_include(request), "application-statuses"})
        payload["included"] = self._build_included([obj], include_rels, request)
        return Response(payload)

    def create(self, request):
        response = super().create(request)
        # Auto-create the initial JobApplicationStatus so every application
        # has at least one status entry from the moment it is created.
        if response.status_code == 201:
            app_id = (response.data.get("data") or {}).get("id")
            if app_id:
                app = JobApplication.objects.filter(pk=int(app_id)).first()
                if app and not JobApplicationStatus.objects.filter(application_id=app.id).exists():
                    status_label = app.status or "Unvetted"
                    status_obj, _ = Status.objects.get_or_create(
                        status=status_label,
                        defaults={"status_type": "application"},
                    )
                    from django.utils import timezone
                    JobApplicationStatus.objects.create(
                        application=app,
                        status=status_obj,
                        logged_at=timezone.now(),
                    )
        return response

    def _upsert(self, request, pk, partial=False):
        obj = self._get_obj(pk)
        if not obj or obj.user_id != request.user.id:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        old_status = obj.status
        response = super()._upsert(request, pk, partial=partial)
        if response.status_code == 200:
            obj.refresh_from_db()
            if obj.status and obj.status != old_status:
                from django.utils import timezone
                status_obj, _ = Status.objects.get_or_create(
                    status=obj.status,
                    defaults={"status_type": "application"},
                )
                JobApplicationStatus.objects.create(
                    application=obj,
                    status=status_obj,
                    logged_at=timezone.now(),
                )
        return response

    def pre_save_payload(self, request, attrs, creating):
        """Automatically set user_id and company_id when creating applications"""
        if creating:
            # Set user_id from authenticated user
            attrs["user_id"] = request.user.id

            # Set company_id from job_post if job_post_id is provided
            job_post_id = attrs.get("job_post_id")
            if job_post_id:
                job_post = JobPost.objects.filter(pk=job_post_id).first()
                if job_post and hasattr(job_post, "company_id") and job_post.company_id:
                    attrs["company_id"] = job_post.company_id

        return attrs

    @extend_schema(
        tags=["Job Applications"],
        summary="List application statuses for a job application",
        responses={200: _JSONAPI_LIST},
    )
    @action(detail=True, methods=["get"], url_path="application-statuses")
    def application_statuses(self, request, pk=None):
        app = JobApplication.objects.filter(pk=int(pk)).first()
        if not app or app.user_id != request.user.id:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = JobApplicationStatusSerializer()
        items = list(JobApplicationStatus.objects.filter(application_id=int(pk)))
        data = [ser.to_resource(i) for i in items]

        # Build included only when ?include=... is provided
        include_rels = self._parse_include(request)

        payload = {"data": data}
        if include_rels:
            payload["included"] = self._build_included(
                items, include_rels, request, primary_serializer=ser
            )
        return Response(payload)

    @extend_schema(
        tags=["Job Applications"],
        summary="List questions for a job application",
        responses={200: _JSONAPI_LIST},
    )
    @action(detail=True, methods=["get"])
    def questions(self, request, pk=None):
        app = JobApplication.objects.filter(pk=int(pk)).first()
        if not app or app.user_id != request.user.id:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = QuestionSerializer()
        items = list(Question.objects.filter(application_id=int(pk)))
        data = [ser.to_resource(i) for i in items]

        # Build included only when ?include=... is provided
        include_rels = self._parse_include(request)

        payload = {"data": data}
        if include_rels:
            payload["included"] = self._build_included(
                items, include_rels, request, primary_serializer=ser
            )
        return Response(payload)


@extend_schema_view(
    list=extend_schema(tags=["Statuses"], summary="List statuses"),
    retrieve=extend_schema(tags=["Statuses"], summary="Retrieve a status"),
    create=extend_schema(tags=["Statuses"], summary="Create a status"),
    update=extend_schema(tags=["Statuses"], summary="Update a status"),
    partial_update=extend_schema(
        tags=["Statuses"], summary="Partially update a status"
    ),
    destroy=extend_schema(tags=["Statuses"], summary="Delete a status"),
)
class StatusViewSet(viewsets.ModelViewSet):
    queryset = Status.objects.all()
    serializer_class = StatusSerializer
    permission_classes = [IsAuthenticated, IsGuestReadOnly]

    def create(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") or {}
        attrs = node.get("attributes") or {}
        relationships = node.get("relationships") or {}

        # If a job_application relationship is present, create a JobApplicationStatus
        app_rel = relationships.get("job_application") or relationships.get("job-application")
        app_rel_data = (app_rel or {}).get("data") or {}
        application_id = app_rel_data.get("id")

        if application_id is not None:
            try:
                application_id = int(application_id)
            except (TypeError, ValueError):
                return Response(
                    {"errors": [{"detail": "Invalid job_application id"}]},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            application = JobApplication.objects.filter(pk=application_id).first()
            if not application:
                return Response(
                    {"errors": [{"detail": "Job application not found"}]},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            status_label = attrs.get("status", "").strip()
            if not status_label:
                return Response(
                    {"errors": [{"detail": "attributes.status is required"}]},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            status_obj, _ = Status.objects.get_or_create(
                status=status_label,
                defaults={"status_type": "application"},
            )

            from django.utils import timezone
            note = attrs.get("note")
            logged_at_raw = attrs.get("logged_at")
            if logged_at_raw:
                try:
                    from dateutil import parser as dateutil_parser
                    logged_at = dateutil_parser.parse(str(logged_at_raw))
                except (ValueError, TypeError):
                    logged_at = timezone.now()
            else:
                logged_at = timezone.now()

            app_status = JobApplicationStatus.objects.create(
                application=application,
                status=status_obj,
                note=note,
                logged_at=logged_at,
            )

            ser = JobApplicationStatusSerializer()
            return Response(
                {"data": ser.to_resource(app_status)},
                status=status.HTTP_201_CREATED,
            )

        # No job_application relationship — create a plain Status lookup record
        return super().create(request)


@extend_schema_view(
    list=extend_schema(
        tags=["Job Application Statuses"], summary="List job application statuses"
    ),
    retrieve=extend_schema(
        tags=["Job Application Statuses"], summary="Retrieve a job application status"
    ),
    create=extend_schema(
        tags=["Job Application Statuses"], summary="Create a job application status"
    ),
    update=extend_schema(
        tags=["Job Application Statuses"], summary="Update a job application status"
    ),
    partial_update=extend_schema(
        tags=["Job Application Statuses"],
        summary="Partially update a job application status",
    ),
    destroy=extend_schema(
        tags=["Job Application Statuses"], summary="Delete a job application status"
    ),
)
class JobApplicationStatusViewSet(BaseViewSet):
    model = JobApplicationStatus
    serializer_class = JobApplicationStatusSerializer

    def create(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") or {}
        attrs = node.get("attributes") or {}
        relationships = node.get("relationships") or {}

        # Resolve application FK
        app_rel = (
            relationships.get("application")
            or relationships.get("job_application")
            or relationships.get("job-application")
        )
        app_rel_data = (app_rel or {}).get("data") or {}
        application_id = app_rel_data.get("id")
        if application_id is None:
            return Response(
                {"errors": [{"detail": "relationships.application is required"}]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            application_id = int(application_id)
        except (TypeError, ValueError):
            return Response(
                {"errors": [{"detail": "Invalid application id"}]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        application = JobApplication.objects.filter(pk=application_id).first()
        if not application:
            return Response(
                {"errors": [{"detail": "Job application not found"}]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Resolve status: prefer relationship FK, fall back to text label in attributes
        status_rel = relationships.get("status") or {}
        status_rel_data = (status_rel or {}).get("data") or {}
        status_rel_id = status_rel_data.get("id")

        if status_rel_id is not None:
            status_obj = Status.objects.filter(pk=int(status_rel_id)).first()
        else:
            status_label = (attrs.get("status") or "").strip()
            if not status_label:
                return Response(
                    {"errors": [{"detail": "attributes.status or relationships.status is required"}]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            status_obj, _ = Status.objects.get_or_create(
                status=status_label,
                defaults={"status_type": "application"},
            )

        from django.utils import timezone
        note = attrs.get("note")
        logged_at_raw = attrs.get("logged_at")
        if logged_at_raw:
            try:
                from dateutil import parser as dateutil_parser
                logged_at = dateutil_parser.parse(str(logged_at_raw))
            except (ValueError, TypeError):
                logged_at = timezone.now()
        else:
            logged_at = timezone.now()

        app_status = JobApplicationStatus.objects.create(
            application=application,
            status=status_obj,
            note=note,
            logged_at=logged_at,
        )

        # Keep the parent application's status field in sync
        application.status = status_obj.status
        application.save(update_fields=["status"])

        ser = JobApplicationStatusSerializer()
        return Response(
            {"data": ser.to_resource(app_status)},
            status=status.HTTP_201_CREATED,
        )

