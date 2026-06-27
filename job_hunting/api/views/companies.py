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
from ._sorting import (
    InvalidSortField,
    parse_sort_fields,
    sort_error_response_body,
)
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

    # Whitelist of explicit `?sort=` fields. The default `relevant`/`-relevant`
    # is handled separately (it's an annotation, not a column). Unknown field
    # -> 400 (not a FieldError 500).
    SORT_FIELDS = frozenset({
        "id",
        "name",
        "display_name",
        "created_at",
        "slug",
    })

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
            # Validate against the whitelist first so an unknown field returns
            # 400 here instead of a FieldError 500 at qs.count() below.
            try:
                sort_fields = parse_sort_fields(sort_param, self.SORT_FIELDS)
            except InvalidSortField as e:
                return Response(
                    sort_error_response_body(e),
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if sort_fields:
                qs = qs.order_by(*sort_fields)

        total = qs.count()
        page_number, page_size = self._page_params()
        total_pages = math.ceil(total / page_size) if page_size else 1
        offset = (page_number - 1) * page_size
        items = list(qs.all()[offset: offset + page_size])

        ser = self.get_serializer()
        # Per-company counts are opt-in via `?meta=counts` (mirrors the
        # resume counts-in-meta gate). Computing them unconditionally
        # would add N×5 `.count()` queries to every list call — most
        # callers don't need the badges, so we only pay the cost when
        # asked. Keys are plural to match `retrieve()` + the frontend
        # `company` model (`jobApplicationsCount`, ...).
        want_counts = self._meta_counts_requested(request)
        data = []
        for obj in items:
            resource = ser.to_resource(obj)
            if want_counts:
                resource["meta"] = self._build_counts(obj)
            data.append(resource)
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
        resource["meta"] = self._build_counts(obj)
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
        # Phase 6a — staff-only toggle for the federation opt-in. Kept
        # behind the staff gate (per the plan's Q2 — Companies should
        # not auto-publish to the fediverse until an employer or
        # operator explicitly opts the row in). The slug is read-only
        # at the API surface; backfill / staff seeds it.
        if "federation_enabled" in attrs and request.user.is_staff:
            obj.federation_enabled = bool(attrs["federation_enabled"])
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
            qs = JobPost.objects.filter(company_id=pk)
        else:
            qs = JobPost.objects.filter(company_id=pk).filter(
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
        apps = list(JobApplication.objects.filter(company_id=pk, user_id=request.user.id))
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
        qs = Scrape.objects.filter(company_id=pk)
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
                job_post__company_id=pk, user_id=request.user.id
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
            Question.objects.filter(company_id=pk)
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
        # target_id is the target Company NanoID PK (CC-77 #79) — string, not int.
        target_id = str(target_id)
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

    @extend_schema(
        tags=["Companies"],
        summary="Mark this company as an alias of another (staff only)",
        responses={
            200: _JSONAPI_LIST,
            400: None,
            403: None,
            404: None,
        },
    )
    @action(detail=True, methods=["post"], url_path="mark-as-alias-of")
    def mark_as_alias_of(self, request, pk=None):
        """Soft-alias verb — set ``self.canonical_id = target_id``.

        Distinct from ``merge-into`` (destructive cut). This action
        leaves both Company rows in place; the source row simply
        gains a ``canonical_id`` pointer to the target. Phase B work
        will switch JobPost / Scrape ingestion to consult the
        canonical pointer; Phase C drops the legacy ``CompanyAlias``
        model.

        Body shape (plain JSON or JSON:API both accepted):

            {"target_id": <int>}
            {"data": {"attributes": {"target_id": <int>}}}

        Side effects (all in ``Company.mark_as_alias_of``):
        - ``self.canonical_id = target_id``.
        - Re-points every Company currently aliased AT self at the
          new canonical root (one-level invariant).
        - If target is itself an alias, walks to the root canonical.

        Errors:
        - 400 ``target_id`` missing / non-int / equal to self.id.
        - 400 cycle (target.canonical chain loops back to self).
        - 403 caller is not staff.
        - 404 self or target Company id not found.
        """
        if not request.user.is_staff:
            return Response(
                {"errors": [{"detail": "Only staff may alias companies."}]},
                status=status.HTTP_403_FORBIDDEN,
            )

        source = Company.objects.filter(pk=pk).first()
        if not source:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        target_id = self._extract_target_id(request.data)
        if target_id is None:
            return Response(
                {"errors": [{"detail": "target_id is required."}]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # target_id is the target Company NanoID PK (CC-77 #79) — string, not int.
        target_id = str(target_id)

        if not Company.objects.filter(pk=target_id).exists():
            return Response(
                {"errors": [{"detail": "target Company not found."}]},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            source.mark_as_alias_of(target_id)
        except ValueError as exc:
            return Response(
                {"errors": [{"detail": str(exc)}]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ser = self.get_serializer()
        return Response({"data": ser.to_resource(source)}, status=status.HTTP_200_OK)

    @extend_schema(
        tags=["Companies"],
        summary="Promote a Company alias back to canonical (staff only)",
        responses={
            200: _JSONAPI_LIST,
            400: None,
            403: None,
            404: None,
        },
    )
    @action(detail=True, methods=["post"], url_path="unmark-as-alias-of")
    def unmark_as_alias_of(self, request, pk=None):
        """Inverse of ``mark-as-alias-of`` — clear ``self.canonical_id``.

        Closes the Phase A staff-curation loop: promotes an alias Company
        back to a canonical (``canonical_id = NULL``). Used by the
        frontend ``Companies::AliasesPanel`` to undo a misapplied alias.

        No request body required.

        Behavior:
        - 200 with the updated Company resource on success.
        - 400 when the Company is already canonical (``canonical_id IS
          NULL``). A no-op silently succeeding would let staff click
          the wrong row without feedback; explicit 400 surfaces the
          mistake.
        - 403 for non-staff (mirrors ``mark-as-alias-of``).
        - 404 when the Company id is not found.

        Aliases that previously pointed AT this row (during the time it
        was itself an alias) were already re-pointed at the root by
        ``mark_as_alias_of``'s one-hop invariant, so unmarking does not
        need to traverse the reverse relation.
        """
        if not request.user.is_staff:
            return Response(
                {"errors": [{"detail": "Only staff may unmark Company aliases."}]},
                status=status.HTTP_403_FORBIDDEN,
            )

        company = Company.objects.filter(pk=pk).first()
        if not company:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        if company.canonical_id is None:
            return Response(
                {
                    "errors": [
                        {
                            "detail": (
                                "Company is already canonical "
                                "(canonical_id is NULL)."
                            )
                        }
                    ]
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        company.canonical = None
        company.save(update_fields=["canonical"])

        ser = self.get_serializer()
        return Response({"data": ser.to_resource(company)}, status=status.HTTP_200_OK)

    @extend_schema(
        tags=["Companies"],
        summary="List Company aliases (rows whose canonical_id == this id)",
        responses={200: _JSONAPI_LIST},
    )
    @action(detail=True, methods=["get"], url_path="aliases")
    def aliases(self, request, pk=None):
        """Sub-collection: every Company whose ``canonical_id = pk``.

        Returns Company resources (same type as the parent). The
        frontend's ``Companies::AliasesPanel`` (cf-frontend territory)
        consumes this via a sub-collection adapter analogous to
        ``frontend/app/adapters/job-post-duplicate-candidate.js``.
        """
        if not Company.objects.filter(pk=pk).exists():
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        aliases_qs = list(Company.objects.filter(canonical_id=pk))
        ser = self.get_serializer()
        return Response(
            {"data": [ser.to_resource(c) for c in aliases_qs]}
        )

    @staticmethod
    def _build_counts(obj):
        """Per-company badge counts emitted in the resource `meta`.

        Single source of truth shared by `retrieve()` (always) and
        `list()` (gated on `?meta=counts`) so the two paths can't
        drift. The applications count goes through the DIRECT
        `JobApplication.company` FK (`related_name="applications"`)
        rather than joining `job_post__company_id` — applications can
        carry a company without a job_post, and the direct FK is the
        cheaper, correct lookup.
        """
        return {
            "job_posts_count": obj.job_posts.count(),
            "job_applications_count": JobApplication.objects.filter(
                company_id=obj.id
            ).count(),
            "scrapes_count": obj.scrapes.count(),
            "questions_count": obj.questions.count(),
            "scores_count": Score.objects.filter(
                job_post__company_id=obj.id
            ).count(),
        }

    @staticmethod
    def _meta_counts_requested(request):
        """True when the client opted into `?meta=counts` (comma-list
        tolerant). Mirrors `ResumeSerializer._meta_counts_requested`."""
        raw = request.query_params.get("meta")
        if not raw:
            return False
        return "counts" in {s.strip() for s in str(raw).split(",") if s.strip()}

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

