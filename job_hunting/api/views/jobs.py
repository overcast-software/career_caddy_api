import logging
import math

from datetime import timedelta

from django.db import transaction
from django.db.models import Q, F, OuterRef, Subquery
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
    JobPostDuplicateCandidateSerializer,
    ScoreSerializer,
    ScrapeSerializer,
    CoverLetterSerializer,
    JobApplicationSerializer,
    SummarySerializer,
    QuestionSerializer,
    StatusSerializer,
    JobApplicationStatusSerializer,
    compute_duplicate_candidates,
)
from job_hunting.api.permissions import IsGuestReadOnly
from job_hunting.lib.ai_client import get_client
from job_hunting.lib.job_post_merge import merge_empty_fields_from_attrs
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


logger = logging.getLogger(__name__)


def _attach_active_application_status(job_posts, user_id):
    """Pre-attach `_active_application_status` (string or None) and
    `_active_reason_code` on each JobPost: the latest
    JobApplicationStatus.status name + reason_code on the user's own
    application for that post. One query for the whole batch.

    reason_code is per-user by construction — sourced from the row whose
    application.user_id == user_id. Never leaks across tenants."""
    if not job_posts or not user_id:
        for jp in job_posts:
            jp._active_application_status = None
            jp._active_reason_code = None
            jp._active_reason_note = None
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
    reason_map = {}
    note_map = {}
    for jas in rows:
        pid = jas.application.job_post_id
        if pid in status_map:
            continue
        status_map[pid] = jas.status.status if jas.status_id else None
        reason_map[pid] = jas.reason_code
        note_map[pid] = jas.note
    for jp in job_posts:
        jp._active_application_status = status_map.get(jp.id)
        jp._active_reason_code = reason_map.get(jp.id)
        jp._active_reason_note = note_map.get(jp.id)


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

    @staticmethod
    def _snapshot_duplicate_signals(post, request):
        """Freeze the candidate signal set for `post` at decision time so
        the dedupe-feedback report can ask whether any automatic signal
        was already firing when the human reached for the verb. Capture
        before the duplicate_of mutation applies, since
        compute_duplicate_candidates filters by the current chain."""
        items = compute_duplicate_candidates(post, request)
        return {
            "candidates": [
                {
                    "id": it.id,
                    "confidence": getattr(it, "_confidence", None),
                    "signals": list(getattr(it, "_match_signals", [])),
                }
                for it in items
            ],
        }

    @staticmethod
    def _record_duplicate_annotation(
        *, from_jp, to_jp_id, previous_to_id, action, request, signal_state
    ):
        from job_hunting.models import DuplicateAnnotation
        DuplicateAnnotation.objects.create(
            from_jp_id=from_jp.id,
            to_jp_id=to_jp_id,
            previous_to_id=previous_to_id,
            action=action,
            set_by=request.user if request.user.is_authenticated else None,
            signal_state=signal_state,
        )

    @staticmethod
    def _visible_jobpost_qs(request):
        """Posts the caller can reach via the five-clause visibility filter
        (created / applied / scored / scraped / discovered). Staff see all.
        Mirrors list() and compute_duplicate_candidates so dup-verb authz
        matches what the user can otherwise see in the UI."""
        if request.user.is_staff:
            return JobPost.objects.all()
        return JobPost.objects.filter(
            Q(created_by_id=request.user.id)
            | Q(applications__user_id=request.user.id)
            | Q(scores__user_id=request.user.id)
            | Q(scrapes__created_by_id=request.user.id)
            | Q(discoveries__user_id=request.user.id)
        ).distinct()

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
        # JobPost is a shared resource (see JobPost model docstring).
        # Staff users see every post; regular users see only posts they
        # have a per-user signal on (created, applied, scored, scraped,
        # or discovered via ingestion).
        #
        # Exception: filter[link] is a *global* canonical-link lookup,
        # used by the browser extension's popup-open "is this URL already
        # tracked?" check. The user is providing the specific URL — this
        # isn't enumeration of someone else's library, it's duplicate
        # detection. Bypass the per-user-signal filter so a JobPost
        # someone else created is still visible when the user asks
        # specifically for it.
        #
        # Match across three URL fields, since the user may have navigated
        # directly to a stored apply destination (e.g. an ATS landing page
        # like allstateinsurance.contacthr.com/151916501 — see JP 1329).
        # Without the apply_url leg, the popup would say "not tracked" on
        # a URL we already know about, and the user would re-scrape it.
        #
        # NULL safety: SQL `=` never matches NULL, so a stored
        # `apply_url IS NULL` row can't false-match against the query URL.
        # No explicit IS NOT NULL guard needed.
        link_filter = request.query_params.get("filter[link]")
        if link_filter is not None:
            from job_hunting.models.job_post_dedupe import canonicalize_link
            canonical = canonicalize_link(link_filter)
            # Build the OR-clause defensively. `canonicalize_link("")`
            # returns None; `Q(field=None)` translates to `field IS NULL`,
            # which would over-match any row whose canonical_link or
            # apply_url is NULL (the common case for apply_url). Drop the
            # canonical legs when the input doesn't yield a canonical
            # form. The exact-match legs against the raw input still run
            # and return the empty set for an empty query, which is the
            # right behavior.
            clauses = Q(link=link_filter) | Q(apply_url=link_filter)
            if canonical:
                clauses |= Q(canonical_link=canonical) | Q(apply_url=canonical)
            qs = JobPost.objects.filter(clauses)
        elif request.user.is_staff:
            qs = JobPost.objects.all()
        else:
            qs = JobPost.objects.filter(
                Q(created_by_id=request.user.id) |
                Q(applications__user_id=request.user.id) |
                Q(scores__user_id=request.user.id) |
                Q(scrapes__created_by_id=request.user.id) |
                Q(discoveries__user_id=request.user.id)
            ).distinct()

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

        # filter[complete]=true|false is the canonical signal — backed
        # by the explicit `JobPost.complete` boolean. filter[stub] stays
        # as a compatibility alias (frontend list view's "stub" toggle):
        # filter[stub]=true ⇔ filter[complete]=false. Three sources flip
        # complete=False: cc_auto email-stub creation, the user clicking
        # "Mark incomplete", and the scrape-graph's ReviewCompleteness
        # rejecting the output.
        complete_filter = request.query_params.get("filter[complete]")
        stub_filter = request.query_params.get("filter[stub]")
        if complete_filter is not None:
            wants_complete = str(complete_filter).lower() in ("1", "true", "yes")
            qs = qs.filter(complete=wants_complete)
        elif stub_filter is not None:
            wants_stub = str(stub_filter).lower() in ("1", "true", "yes")
            qs = qs.filter(complete=not wants_stub)

        # Posting status: default-hide closed posts. Pass
        # ?include_closed=true to opt back in (list-view toggle), or
        # ?filter[posting_status]=closed for an exact match. Posts
        # with NULL status (historical / never-scanned) always show.
        # filter[link] is exempt — it's an identity lookup ("is this
        # URL already tracked?"), not a list view, so the closed-default
        # would hide a stored JP from the extension popup's "incomplete"
        # banner / Tracked screen and silently let a duplicate scrape
        # through.
        posting_status_filter = request.query_params.get("filter[posting_status]")
        include_closed = str(
            request.query_params.get("include_closed") or ""
        ).lower() in ("1", "true", "yes")
        if posting_status_filter is not None:
            qs = qs.filter(posting_status=posting_status_filter)
        elif not include_closed and link_filter is None:
            qs = qs.exclude(posting_status="closed")

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
                | Q(link__icontains=query_filter)
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
        # Visibility mirrors JobPostViewSet.list: any of the five per-user
        # signals grants access. Discovery is the canonical email-ingest
        # signal — without this clause GET /job-posts/<id>/ 404s for a
        # post the user just received via cc_auto.
        has_access = (
            request.user.is_staff or
            obj.created_by_id == request.user.id or
            obj.applications.filter(user_id=request.user.id).exists() or
            obj.scores.filter(user_id=request.user.id).exists() or
            obj.scrapes.filter(created_by_id=request.user.id).exists() or
            obj.discoveries.filter(user_id=request.user.id).exists()
        )
        if not has_access:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        # Scope top_score to request.user — cross-user scores must not leak via
        # shared JobPosts (extension popup-open lookup is the canonical leak path).
        obj._top_score = obj.scores.filter(user_id=request.user.id).order_by("-score").first()
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

        # Phase 2.5 catchall mail provenance. `forwarded_via_address` is a
        # JobPostDiscovery column (per-user, not per-post), but cc_auto
        # POSTs it as a JobPost attribute on the same body. Pull it
        # straight from the raw payload (parse_payload silently drops
        # any attribute not declared on JobPostSerializer, so it would
        # vanish before we got here otherwise), validate the source-
        # pairing invariant, and forward the value into _record_discovery
        # so the discovery row carries it.
        #
        # Pairing rule (serializer-level): forwarded_via_address is
        # *required* when source == "email-forward" and *forbidden* on
        # every other source. Forbidden side prevents silent provenance
        # bleed when a non-mail path echoes a stale field; required side
        # makes the cc_auto contract enforceable from the API.
        raw_attrs = (
            (data.get("data") or {}).get("attributes") or {}
            if isinstance(data, dict)
            else {}
        )
        forwarded_via_address = raw_attrs.get("forwarded_via_address")
        if isinstance(forwarded_via_address, str):
            forwarded_via_address = forwarded_via_address.strip() or None

        # Phase 2.5 staff-on-behalf RBAC. `discover_for_user_id` lets a
        # staff API key (cc_auto's) attribute a JobPostDiscovery to a
        # user other than the authenticated principal. Optional —
        # defaults to request.user.id (self-discover, the common path).
        #
        # Pull from raw payload: it's a routing-only field, not a
        # JobPost column, so JobPostSerializer.parse_payload drops it.
        # Accept both `discover_for_user_id` and the dasherized
        # `discover-for-user-id` (Ember/JSON:API client convention).
        #
        # RBAC: 403 unless the caller is staff OR is targeting themselves.
        # Non-staff attempting to write on behalf of another user is
        # rejected before any DB write. Staff bypass is intentional — the
        # whole point of the field is to let cc_auto's staff key drive
        # writes for any user. Audit lives on the discovery row via the
        # `requested_by_user_id` column (see migration 0095 + model).
        target_user_id_raw = (
            raw_attrs.get("discover_for_user_id")
            or raw_attrs.get("discover-for-user-id")
        )
        target_user_id = None
        if target_user_id_raw is not None:
            try:
                target_user_id = int(target_user_id_raw)
            except (TypeError, ValueError):
                return Response(
                    {"errors": [{
                        "status": "400",
                        "detail": "discover_for_user_id must be an integer",
                    }]},
                    status=400,
                )
        else:
            target_user_id = request.user.id

        if target_user_id != request.user.id and not request.user.is_staff:
            return Response(
                {"errors": [{
                    "status": "403",
                    "detail": (
                        "Only staff may attribute a discovery to another user"
                    ),
                }]},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Resolve the target user — fails fast on a stale id from cc_auto
        # rather than letting the FK fail at INSERT time.
        from django.contrib.auth import get_user_model
        UserModel = get_user_model()
        if target_user_id == request.user.id:
            # Avoid an extra DB hit on the self path (every paste / manual
            # POST that the auth backend already hydrated).
            target_user = request.user
        else:
            target_user = UserModel.objects.filter(id=target_user_id).first()
            if target_user is None:
                return Response(
                    {"errors": [{
                        "status": "400",
                        "detail": (
                            f"discover_for_user_id {target_user_id} does not exist"
                        ),
                    }]},
                    status=400,
                )
        src_for_validation = attrs.get("source") or "manual"
        if src_for_validation == "email-forward":
            if not forwarded_via_address:
                return Response(
                    {"errors": [{
                        "status": "400",
                        "detail": (
                            "forwarded_via_address is required when "
                            "source='email-forward'"
                        ),
                    }]},
                    status=400,
                )
        else:
            if forwarded_via_address:
                return Response(
                    {"errors": [{
                        "status": "400",
                        "detail": (
                            "forwarded_via_address is only valid when "
                            "source='email-forward'"
                        ),
                    }]},
                    status=400,
                )

        date_errors = self._parse_date_attrs(attrs)
        if date_errors:
            return Response(
                {"errors": [{"detail": v} for v in date_errors.values()]}, status=400
            )

        # URL hygiene — Phase 0 of the ingest defense lift to JobPost POST.
        # Mirrors the policy gate POST /scrapes/ already runs (see
        # scrapes.py:377-391). Hard-rejects non-http schemes, our own
        # domain, and private/internal hosts so cc_auto, the chat agent,
        # and the manual create form can't seed JobPost rows with junk
        # URLs. Tracker-redirect resolution is intentionally NOT lifted
        # here in this slice — see todo.org "Ingest abuse defense — Phase 2"
        # and notes.org Scrape Log 2026-05-01 for why
        # (LinkedIn /comm/ HEAD-redirects to a login wall without auth, so
        # naive resolve_tracker would persist worse URLs than the wrapped
        # ones we'd be trying to strip).
        link = attrs.get("link")
        if link:
            from job_hunting.lib.url_policy import (
                UrlPolicyError,
                validate_submission_url,
            )
            try:
                attrs["link"] = validate_submission_url(link)
            except UrlPolicyError as e:
                logger.info(
                    "JobPostViewSet.create: rejecting link=%s — %s (%s)",
                    link, e, e.code,
                )
                return Response(
                    {"errors": [{
                        "status": "422",
                        "code": e.code,
                        "detail": str(e),
                    }]},
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )

        # Inbound `complete` gating (Posture E). cc_auto's email pipeline
        # creates thin stubs (title + company + link, no description) and
        # declares complete=False up front so the post enters the existing
        # incomplete-recovery path. Honor only when the source is at or
        # below the email trust tier — extension/scrape data is meant to be
        # authoritative and an inbound False from them is a bug or attack.
        # Inbound True is never honored: that flip is the api's job
        # (parse_scrape / ReviewCompleteness), not an external client's.
        if "complete" in attrs:
            from job_hunting.models.job_post_dedupe import (
                SOURCE_TRUST,
                source_trust,
            )
            inbound = attrs["complete"]
            src = attrs.get("source") or "manual"
            if not (inbound is False and source_trust(src) <= SOURCE_TRUST["email"]):
                attrs.pop("complete", None)

        from job_hunting.models import JobPostDiscovery

        def _record_discovery(post):
            # `user` is the target (who gets the visibility signal);
            # `requested_by` is who drove the write (the authenticated
            # principal — equals target on every self-discover path,
            # differs on a staff-on-behalf write). Audit chain lives in
            # the discovery row, not a side table.
            JobPostDiscovery.objects.get_or_create(
                job_post=post,
                user=target_user,
                defaults={
                    "source": attrs.get("source") or "manual",
                    # Only populated when source=='email-forward' (validated
                    # above); other sources arrive here with None and the
                    # column stays NULL.
                    "forwarded_via_address": forwarded_via_address,
                    "requested_by": request.user,
                },
            )

        if attrs.get("link"):
            existing = JobPost.objects.filter(link=attrs["link"]).first()
            if existing:
                # Backfill empty fields from the incoming POST. JobPost is
                # universal: cc_auto's email path commonly hits a row that
                # an earlier scrape created as a stub (link known, company
                # NULL, title thin). Without this merge the new association
                # is silently dropped — the post stays off
                # /companies/<id>/job-posts even though we now know the
                # company. Only ever fills NULLs; never overwrites an
                # existing value (a wrong cc_auto guess shouldn't clobber
                # a good prior association).
                merge_empty_fields_from_attrs(existing, attrs)
                _record_discovery(existing)
                return Response({"data": ser.to_resource(existing)}, status=status.HTTP_200_OK)
        obj = JobPost(**attrs)
        # Populate dedupe fields pre-save so find_duplicate sees them.
        from job_hunting.models.job_post_dedupe import (
            canonicalize_link,
            find_duplicate,
            fingerprint,
            normalized_fingerprint,
        )
        obj.canonical_link = canonicalize_link(obj.link)
        obj.content_fingerprint = fingerprint(obj)
        obj.normalized_fingerprint = normalized_fingerprint(obj)
        dupe = find_duplicate(obj)
        if dupe:
            merge_empty_fields_from_attrs(dupe, attrs)
            _record_discovery(dupe)
            return Response({"data": ser.to_resource(dupe)}, status=status.HTTP_200_OK)
        obj.save()
        if not obj.posted_date:
            obj.posted_date = obj.created_at.date()
            obj.save(update_fields=["posted_date"])
        _record_discovery(obj)
        return Response({"data": ser.to_resource(obj)}, status=status.HTTP_201_CREATED)

    def update(self, request, pk=None):
        return self._upsert_django(request, pk, partial=False)

    def partial_update(self, request, pk=None):
        return self._upsert_django(request, pk, partial=True)

    def _upsert_django(self, request, pk, partial=False):
        obj = JobPost.objects.filter(pk=pk).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        # Staff bypass for the edit form (matches destroy() above and the
        # Phase 1 ticket for jp.edit: created_by + staff may PATCH; everyone
        # else gets 403). Revisit if usage shows pain — see Plans/PLAN
        # ActivityPub prep + job-post adaptation/Job-post edit page additions.
        if not request.user.is_staff and obj.created_by_id != request.user.id:
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
        # When the caller edits `link`, refresh canonical_link from the new
        # raw URL. Save()'s "set once" guard skips the derivation when
        # canonical_link is already populated, which is correct on the
        # create path (callers may seed an explicit canonical_link for
        # post-redirect dedup — see the email-stub upgrade regression at
        # tests/test_job_post_extractor.py::
        # test_canonical_link_hit_upgrades_email_stub_at_redirected_url).
        # On a PATCH that changes the link, however, the form expects the
        # canonical_link readonly display to refresh — so we explicitly
        # clear it here, and save() then re-runs canonicalize_link().
        if "link" in attrs and attrs["link"] != obj.link:
            obj.canonical_link = None
            attrs.pop("canonical_link", None)
        date_errors = self._parse_date_attrs(attrs)
        if date_errors:
            return Response(
                {"errors": [{"detail": v} for v in date_errors.values()]}, status=400
            )
        prev_company_id = obj.company_id
        for k, v in attrs.items():
            setattr(obj, k, v)
        obj.save()
        if not obj.posted_date:
            obj.posted_date = obj.created_at.date()
            obj.save(update_fields=["posted_date"])
        # When the post moves to a different Company (typo correction is
        # the dominant case in multi-tenant), cascade the FK to the four
        # child tables that carry their own company_id. Bulk UPDATE skips
        # signals/save() — the children's textual content is left alone
        # (Q&A / cover letters / applications are write-once-and-forget
        # in practice; rewriting bodies is out of scope).
        if obj.company_id != prev_company_id:
            with transaction.atomic():
                Question.objects.filter(job_post_id=obj.id).update(
                    company_id=obj.company_id
                )
                Scrape.objects.filter(job_post_id=obj.id).update(
                    company_id=obj.company_id
                )
                CoverLetter.objects.filter(job_post_id=obj.id).update(
                    company_id=obj.company_id
                )
                JobApplication.objects.filter(job_post_id=obj.id).update(
                    company_id=obj.company_id
                )
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
        reason_code = (data.get("reason_code") or "").strip() or None
        allowed = {"Vetted Good", "Vetted Bad"}
        if status_name not in allowed:
            return Response(
                {"errors": [{"detail": f"status must be one of {sorted(allowed)}"}]},
                status=400,
            )

        from job_hunting.lib.vetting_reasons import VETTING_REASON_CODES
        if status_name != "Vetted Bad":
            reason_code = None
        elif reason_code is not None:
            if reason_code not in VETTING_REASON_CODES:
                return Response(
                    {"errors": [{"detail": f"reason_code must be one of {sorted(VETTING_REASON_CODES)}"}]},
                    status=400,
                )
            if reason_code == "other" and not note:
                return Response(
                    {"errors": [{"detail": "reason_code 'other' requires a non-empty note"}]},
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
            reason_code=reason_code,
        )
        if app.status != status_name:
            app.status = status_name
            app.save(update_fields=["status"])

        obj._active_application_status = status_name
        obj._active_reason_code = reason_code
        obj._active_reason_note = note if reason_code == "other" else None
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

    @action(detail=True, methods=["post"], url_path="resolve-and-dedupe")
    def resolve_and_dedupe(self, request, pk=None):
        """Staff-only: kick off a browser-driven scrape that resolves
        redirects + captures the apply URL, but skips LLM extraction.

        Creates a Scrape with ``skip_extract=True, status="hold"`` linked
        to this JobPost and pointing at ``self.link``. The hold-poller
        picks it up and the scrape-graph runs Navigate → ResolveFinalUrl
        → CheckLinkDedup → (page-load) → ResolveApplyUrl → End. If the
        resolved URL canonical-matches an existing JobPost, the
        DuplicateShortCircuit branch fires (existing graph behavior) and
        the new scrape attaches there. Otherwise the scrape ends with
        ``apply_url`` set on the Scrape and no extraction performed —
        the originating JobPost's fields are intentionally untouched.

        Use case: a tracker-URL stub like ZipRecruiter ``/km/<token>``
        whose canonical_link can't be derived without a browser fetch
        that follows the JS / meta-refresh redirect. Combined with
        parse_scrape's canonical_link OR-leg dedup, the resulting child
        scrape collapses onto the canonical JobPost row automatically.

        Returns the new Scrape so the client can poll for terminal."""
        if not request.user.is_staff:
            return Response(
                {"errors": [{"detail": "Staff only"}]}, status=403
            )
        obj = JobPost.objects.filter(pk=pk).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        if not obj.link:
            return Response(
                {"errors": [{
                    "detail": "JobPost has no link to resolve",
                    "code": "no_link",
                }]},
                status=400,
            )

        scrape = Scrape.objects.create(
            url=obj.link,
            status="hold",
            created_by=request.user,
            job_post=obj,
            company=obj.company if obj.company_id else None,
            source="manual",
            skip_extract=True,
        )
        from job_hunting.lib.scraper import _log_scrape_status
        _log_scrape_status(
            scrape.id, "hold", note="resolve-and-dedupe (staff)"
        )

        scr_ser = ScrapeSerializer()
        return Response(
            {"data": scr_ser.to_resource(scrape)},
            status=status.HTTP_202_ACCEPTED,
        )

    @extend_schema(
        tags=["Job Posts"],
        summary="List job posts marked as duplicates of this one",
        responses={200: _JSONAPI_LIST, 404: _JSONAPI_ITEM},
    )
    @action(detail=True, methods=["get"], url_path="duplicates")
    def duplicates(self, request, pk=None):
        """Reverse-FK list: posts whose duplicate_of points at this one.
        Visibility-filtered so the form's manual-dedup panel only shows
        the caller's own siblings."""
        if not JobPost.objects.filter(pk=pk).exists():
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        visible = self._visible_jobpost_qs(request)
        rows = list(visible.filter(duplicate_of_id=int(pk)))
        ser = self.get_serializer()
        return Response({"data": [ser.to_resource(r) for r in rows]})

    @extend_schema(
        tags=["Job Posts"],
        summary="Mark this job post as a duplicate of another",
        request={"application/json": {"type": "object", "properties": {
            "target_id": {"type": "integer"}
        }}},
        responses={200: _JSONAPI_ITEM, 400: _JSONAPI_ITEM, 403: _JSONAPI_ITEM, 404: _JSONAPI_ITEM},
    )
    @action(detail=True, methods=["post"], url_path="mark-duplicate-of")
    def mark_duplicate_of(self, request, pk=None):
        """Set this post's duplicate_of to `target_id`. Caller must have
        BOTH posts in their visibility set (staff bypass). Rejects self-
        target and any chain that would form a cycle."""
        visible = self._visible_jobpost_qs(request)
        post = visible.filter(pk=pk).first()
        if not post:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        data = request.data if isinstance(request.data, dict) else {}
        try:
            target_id = int(data.get("target_id"))
        except (TypeError, ValueError):
            return Response(
                {"errors": [{"detail": "target_id (int) is required"}]},
                status=400,
            )
        if target_id == post.id:
            return Response(
                {"errors": [{"detail": "A post cannot be a duplicate of itself"}]},
                status=400,
            )
        target = visible.filter(pk=target_id).first()
        if not target:
            return Response(
                {"errors": [{"detail": "target not found or not visible"}]},
                status=404,
            )

        # Cycle check: walk target.canonical chain; if we encounter `post`,
        # the assignment would create a loop.
        seen, cur = {post.id}, target
        while cur is not None:
            if cur.id in seen:
                return Response(
                    {"errors": [{"detail": "Would create a duplicate cycle"}]},
                    status=400,
                )
            seen.add(cur.id)
            cur = cur.duplicate_of

        previous_to_id = post.duplicate_of_id
        signals = self._snapshot_duplicate_signals(post, request)
        post.duplicate_of_id = target.id
        post.save(update_fields=["duplicate_of_id"])
        self._record_duplicate_annotation(
            from_jp=post,
            to_jp_id=target.id,
            previous_to_id=previous_to_id,
            action="mark",
            request=request,
            signal_state=signals,
        )
        ser = self.get_serializer()
        return Response({"data": ser.to_resource(post)})

    @extend_schema(
        tags=["Job Posts"],
        summary="Clear this job post's duplicate_of pointer",
        responses={200: _JSONAPI_ITEM, 403: _JSONAPI_ITEM, 404: _JSONAPI_ITEM},
    )
    @action(detail=True, methods=["post"], url_path="unlink-duplicate")
    def unlink_duplicate(self, request, pk=None):
        """Set this post's duplicate_of to NULL. Idempotent — no-op when
        it was already null. Visibility-gated on the post itself."""
        visible = self._visible_jobpost_qs(request)
        post = visible.filter(pk=pk).first()
        if not post:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        if post.duplicate_of_id is not None:
            previous_to_id = post.duplicate_of_id
            signals = self._snapshot_duplicate_signals(post, request)
            post.duplicate_of_id = None
            post.save(update_fields=["duplicate_of_id"])
            self._record_duplicate_annotation(
                from_jp=post,
                to_jp_id=None,
                previous_to_id=previous_to_id,
                action="unlink",
                request=request,
                signal_state=signals,
            )
        ser = self.get_serializer()
        return Response({"data": ser.to_resource(post)})

    @extend_schema(
        tags=["Job Posts"],
        summary="Promote this post to the canonical row of its dup cluster",
        responses={200: _JSONAPI_ITEM, 400: _JSONAPI_ITEM, 403: _JSONAPI_ITEM, 404: _JSONAPI_ITEM},
    )
    @action(detail=True, methods=["post"], url_path="promote-canonical")
    def promote_canonical(self, request, pk=None):
        """Swap roles: this post becomes canonical, the previous canonical
        becomes a duplicate of this post, and every sibling that pointed at
        the old canonical re-points at the new one. Only meaningful when
        the post currently has duplicate_of set; otherwise 400."""
        visible = self._visible_jobpost_qs(request)
        post = visible.filter(pk=pk).first()
        if not post:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        if post.duplicate_of_id is None:
            return Response(
                {"errors": [{"detail": "Post is not a duplicate of anything"}]},
                status=400,
            )

        old_canonical_id = post.duplicate_of_id
        signals = self._snapshot_duplicate_signals(post, request)
        with transaction.atomic():
            # Re-point every sibling (rows whose duplicate_of_id == old root)
            # at the new canonical, then clear self and flip the old root.
            JobPost.objects.filter(
                duplicate_of_id=old_canonical_id
            ).exclude(pk=post.id).update(duplicate_of_id=post.id)
            post.duplicate_of_id = None
            post.save(update_fields=["duplicate_of_id"])
            JobPost.objects.filter(pk=old_canonical_id).update(
                duplicate_of_id=post.id
            )
            self._record_duplicate_annotation(
                from_jp=post,
                to_jp_id=None,
                previous_to_id=old_canonical_id,
                action="promote",
                request=request,
                signal_state=signals,
            )
        ser = self.get_serializer()
        return Response({"data": ser.to_resource(post)})

    @extend_schema(
        tags=["Job Posts"],
        summary="ActivityStreams 2.0 JSON-LD adapter (federation prep)",
        description=(
            "Returns the JobPost as an AS2 Note object so future "
            "ActivityPub consumers can ingest it. Phase 4 federation "
            "prep — no Outbox, no HTTP signatures, no dispatch yet. "
            "Visibility-scoped: a regular user only sees rows they "
            "could see in the normal list view."
        ),
        responses={200: OpenApiResponse(description="AS2 Note JSON-LD")},
    )
    @action(detail=True, methods=["get"], url_path="as-object")
    def as_object(self, request, pk=None):
        from job_hunting.lib.as_object import job_post_as_object
        post = self._visible_jobpost_qs(request).filter(pk=pk).first()
        if not post:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        return Response(job_post_as_object(post), content_type="application/activity+json")

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
        summary="List likely-duplicate JobPosts for this post",
        description=(
            "Returns peer JobPosts the system suspects represent the same role.\n\n"
            "Three signal classes:\n"
            "  - canonical_link: same canonicalized URL.\n"
            "  - fingerprint: same content_fingerprint (company + normalized title + location).\n"
            "  - title_similarity: same company + one title is a prefix/suffix of the other,\n"
            "    catching the suffix-drift case fingerprint can't (e.g. 'X' vs 'X 75-100% FTE').\n\n"
            "Excludes self and any post in this jp's duplicate_of chain.\n"
            "Visibility-scoped: regular users only see candidates they themselves can see\n"
            "(staff sees all). Empty list when nothing surfaces.\n"
        ),
        responses={200: _JSONAPI_LIST},
    )
    @action(detail=True, methods=["get"], url_path="duplicate-candidates")
    def duplicate_candidates(self, request, pk=None):
        post = JobPost.objects.filter(pk=pk).first()
        if not post:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        # Computation lives in serializers.compute_duplicate_candidates so the
        # ?include=duplicate-candidates sideload path on jp.show fetches the
        # exact same set in the same shape — one source of truth.
        items = compute_duplicate_candidates(post, request)
        ser = JobPostDuplicateCandidateSerializer()
        return Response({"data": [ser.to_resource(it) for it in items]})

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

        total = qs.count()
        page_number, page_size = self._page_params()
        total_pages = math.ceil(total / page_size) if page_size else 1
        offset = (page_number - 1) * page_size
        items = list(qs.all()[offset: offset + page_size])

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

