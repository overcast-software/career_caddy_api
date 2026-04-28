import logging
import math

from django.db.models import F, Q
from django.conf import settings
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated, IsAdminUser
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
    _PAGE_PARAMS,
    _SORT_PARAM,
    _JSONAPI_ITEM,
)
from ..serializers import ScrapeSerializer, ScrapeProfileSerializer
from job_hunting.lib.scraper import Scraper
from job_hunting.models import (
    JobPost,
    Scrape,
    ScrapeProfile,
)
from job_hunting.lib.services.application_flow import _is_thin_description

logger = logging.getLogger(__name__)


@extend_schema_view(
    list=extend_schema(
        tags=["Scrapes"],
        summary="List scrapes",
        parameters=_PAGE_PARAMS + [_SORT_PARAM],
    ),
    update=extend_schema(tags=["Scrapes"], summary="Update a scrape"),
    partial_update=extend_schema(tags=["Scrapes"], summary="Partially update a scrape"),
    destroy=extend_schema(tags=["Scrapes"], summary="Delete a scrape"),
)
class ScrapeViewSet(BaseViewSet):
    model = Scrape
    serializer_class = ScrapeSerializer

    def list(self, request):
        qs = Scrape.objects.filter(
            Q(created_by=request.user)
            | Q(job_post__created_by_id=request.user.id)
            | Q(job_post__isnull=True, created_by__isnull=True)
        )

        # Sorting — explicit sort wins, with -id as deterministic tiebreak so
        # rows sharing the same sort-key value (e.g. several scrapes created
        # in the same second, or sorting by status) don't reshuffle across
        # pages. Default sort is created_at DESC nulls-last, then -id.
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
            if sort_fields and "id" not in sort_field_names:
                sort_fields.append(F("id").desc())
            if sort_fields:
                qs = qs.order_by(*sort_fields)
        else:
            # Scrape has no created_at; scraped_at is nullable (pending/hold
            # rows). Default: scraped_at DESC nulls-last, then -id so newest
            # completed scrapes lead, unfinished ones sit below, and same-
            # timestamp rows tie-break deterministically by id.
            qs = qs.order_by(F("scraped_at").desc(nulls_last=True), F("id").desc())

        # Status filter
        status_filter = request.query_params.get("filter[status]")
        if status_filter:
            qs = qs.filter(status=status_filter)

        # has_score filter — scope to scrapes whose linked JobPost either has
        # or lacks a Score record. Drives the auto-score daemon's candidate
        # list: it polls completed+has_score=false to find new posts to score.
        has_score = request.query_params.get("filter[has_score]")
        if has_score is not None:
            wants = str(has_score).lower() in ("1", "true", "yes")
            from job_hunting.models import Score

            scored_post_ids = Score.objects.values_list(
                "job_post_id", flat=True
            ).distinct()
            if wants:
                qs = qs.filter(job_post_id__in=scored_post_ids)
            else:
                qs = qs.exclude(job_post_id__in=scored_post_ids).exclude(
                    job_post_id__isnull=True
                )

        # Free-text search across URL, job-post title, company name, and content.
        query_filter = request.query_params.get("filter[query]")
        if query_filter:
            qs = qs.filter(
                Q(url__icontains=query_filter)
                | Q(job_content__icontains=query_filter)
                | Q(job_post__title__icontains=query_filter)
                | Q(job_post__company__name__icontains=query_filter)
                | Q(job_post__company__display_name__icontains=query_filter)
            ).distinct()

        # Pagination
        total = qs.count()
        page_number, page_size = self._page_params()
        total_pages = math.ceil(total / page_size) if page_size else 1
        offset = (page_number - 1) * page_size
        items = list(qs.all()[offset : offset + page_size])

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

    def pre_save_payload(self, request, attrs, creating=False):
        attrs = super().pre_save_payload(request, attrs, creating=creating)
        if "html" in attrs and attrs["html"] and not attrs.get("job_content"):
            from job_hunting.lib.scrapers.html_cleaner import clean_html_to_markdown
            attrs["job_content"] = clean_html_to_markdown(attrs["html"])
        if attrs.get("job_content"):
            from job_hunting.lib.scrapers.html_cleaner import strip_agent_chat
            attrs["job_content"] = strip_agent_chat(attrs["job_content"])
        if attrs.get("status") == "completed" and not attrs.get("scraped_at"):
            from django.utils import timezone
            attrs["scraped_at"] = timezone.now()
        return attrs

    def _sync_associations(self, pk):
        """After an update, ensure company_id mirrors the job post's company."""
        scrape = Scrape.objects.filter(pk=int(pk)).first()
        if not scrape:
            return
        if scrape.job_post_id and not scrape.company_id:
            jp = JobPost.objects.filter(pk=scrape.job_post_id).first()
            if jp and jp.company_id:
                scrape.company_id = jp.company_id
                scrape.save(update_fields=["company_id"])

    def _maybe_trigger_extraction(self, pk, force: bool = False):
        from job_hunting.lib.scraper import _maybe_caddy_extract
        scrape = Scrape.objects.filter(pk=int(pk)).first()
        if scrape:
            _maybe_caddy_extract(scrape, force=force)

    def _check_scrape_ownership(self, request, pk):
        obj = Scrape.objects.filter(pk=int(pk)).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        if not request.user.is_staff:
            if obj.created_by_id and obj.created_by_id != request.user.id:
                return Response({"errors": [{"detail": "Not found"}]}, status=404)
            if obj.job_post_id and obj.job_post.created_by_id and obj.job_post.created_by_id != request.user.id:
                return Response({"errors": [{"detail": "Not found"}]}, status=404)
        return None

    def update(self, request, pk=None):
        denied = self._check_scrape_ownership(request, pk)
        if denied:
            return denied
        response = super().update(request, pk=pk)
        self._sync_associations(pk)
        self._maybe_trigger_extraction(pk, force=self._reparse_force(pk))
        return response

    def partial_update(self, request, pk=None):
        denied = self._check_scrape_ownership(request, pk)
        if denied:
            return denied
        # Capture status before update for change detection
        old_status = None
        obj = Scrape.objects.filter(pk=int(pk)).first()
        if obj:
            old_status = obj.status
        response = super().partial_update(request, pk=pk)
        self._sync_associations(pk)
        self._maybe_trigger_extraction(pk, force=self._reparse_force(pk))
        # Log status change to history
        if obj:
            obj.refresh_from_db()
            if obj.status != old_status:
                data = request.data if isinstance(request.data, dict) else {}
                node = data.get("data") or {}
                attrs = node.get("attributes") or {}
                note = attrs.get("note")
                from job_hunting.lib.scraper import _log_scrape_status
                _log_scrape_status(int(pk), obj.status, note=note)
        return response

    def _reparse_force(self, pk) -> bool:
        """Force re-parse when a scrape that's already linked to a JobPost
        transitions back through the extraction pipeline (poller finishing
        a user-triggered re-scrape)."""
        scrape = Scrape.objects.filter(pk=int(pk)).first()
        return bool(scrape and scrape.job_post_id and scrape.status == "completed")

    def destroy(self, request, pk=None):
        denied = self._check_scrape_ownership(request, pk)
        if denied:
            return denied
        Scrape.objects.filter(pk=int(pk)).delete()
        return Response(status=204)

    @extend_schema(
        tags=["Scrapes"],
        summary="Get the current status of a scrape",
        responses={200: _JSONAPI_ITEM, 404: OpenApiResponse(description="Not found")},
    )
    def retrieve(self, request, pk=None):
        """Get the current status of a scrape"""
        obj = Scrape.objects.filter(pk=int(pk)).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        if obj.created_by_id and obj.created_by_id != request.user.id:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        if obj.job_post_id and obj.job_post.created_by_id and obj.job_post.created_by_id != request.user.id:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        ser = self.get_serializer()
        payload = {"data": ser.to_resource(obj)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([obj], include_rels, request)
        return Response(payload)

    @extend_schema(
        tags=["Scrapes"],
        summary="Initiate a URL scrape (async — returns 202 Accepted). Returns existing scrape if URL already processed.",
        request=inline_serializer(
            name="ScrapeCreateRequest",
            fields={"url": drf_serializers.URLField(help_text="URL to scrape")},
        ),
        responses={
            202: OpenApiResponse(description="Scrape started"),
            200: OpenApiResponse(description="Existing scrape returned"),
            400: OpenApiResponse(description="URL missing"),
            501: OpenApiResponse(description="Scraping disabled"),
        },
    )
    def create(self, request):

        # Detect a "url" key in either a plain JSON body or JSON:API attributes
        data = request.data if isinstance(request.data, dict) else {}
        url = data.get("url")
        attrs = {}
        if isinstance(data.get("data"), dict):
            attrs = data["data"].get("attributes") or {}
            if url is None:
                url = attrs.get("url")

        # "hold" status: create the scrape record without dispatching the scraper.
        # Used by MCP agents to queue URLs for later processing.
        req_status = attrs.get("status") or data.get("status")
        if req_status == "hold":
            if not url:
                return Response(
                    {"errors": [{"detail": "URL is required"}]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # Resolve any explicit JobPost relationship the caller sent.
            # Doing this BEFORE the dedupe check lets jp.show's "Run scrape"
            # / "Scrape & Score" actions re-scrape the post they're already
            # viewing — the caller has named the post, so we're not at risk
            # of minting a duplicate. The dedupe gate exists to protect the
            # context-free callers (chat agent, bookmarklet, list-row
            # Scrape & Score), which don't pass a relationship.
            rels = (data.get("data") or {}).get("relationships") or {} if isinstance(data.get("data"), dict) else {}
            jp_rel = rels.get("job-post") or rels.get("job_post")
            linked_jp = None
            if jp_rel and isinstance(jp_rel.get("data"), dict):
                linked_jp = JobPost.objects.filter(pk=int(jp_rel["data"]["id"])).first()

            # Dedupe gate: if no relationship was supplied, refuse to mint a
            # redundant scrape when the URL already maps to a JobPost.
            # Respond 409 with the existing post id in errors[].meta so the
            # chat agent / frontend / extension can navigate the user
            # instead of triggering a re-scrape they didn't ask for.
            #
            # The 409 shape (errors[], no data) matters: the frontend calls
            # this via Ember Data createRecord('scrape').save(). Returning
            # 200 with data.type='job-post' would push a foreign type into
            # the in-flight scrape identifier and collide with an existing
            # job-post:N lid in the store. Errors-only with a non-2xx
            # status keeps Ember Data from touching the store.
            if linked_jp is None:
                from job_hunting.models.job_post_dedupe import canonicalize_link
                canonical = canonicalize_link(url)
                existing_jp = None
                if canonical:
                    existing_jp = JobPost.objects.filter(
                        canonical_link=canonical
                    ).first()
                if existing_jp is None:
                    existing_jp = JobPost.objects.filter(link=url).first()
                if existing_jp is not None:
                    logger.info(
                        "ScrapeViewSet.create: url already maps to job_post id=%s; "
                        "skipping scrape",
                        existing_jp.id,
                    )
                    return Response(
                        {
                            "errors": [
                                {
                                    "status": "409",
                                    "code": "duplicate",
                                    "title": "URL maps to existing job post",
                                    "detail": (
                                        f"URL already maps to job post "
                                        f"#{existing_jp.id}; not creating a scrape."
                                    ),
                                    "meta": {
                                        "existing_job_post_id": existing_jp.id,
                                    },
                                }
                            ]
                        },
                        status=status.HTTP_409_CONFLICT,
                    )

            source = attrs.get("source") or data.get("source") or "scrape"
            scrape = Scrape.objects.create(
                url=url,
                status="hold",
                created_by=request.user,
                source=source,
            )
            from job_hunting.lib.scraper import _log_scrape_status
            _log_scrape_status(scrape.id, "hold")
            # Bind to the JobPost: explicit relationship takes precedence,
            # otherwise fall back to matching by URL. Inherit the job
            # post's company when none supplied.
            if not linked_jp:
                linked_jp = JobPost.objects.filter(link=url).first()
            if linked_jp:
                scrape.job_post = linked_jp
                if not scrape.company_id and linked_jp.company_id:
                    scrape.company_id = linked_jp.company_id
                scrape.save()
            logger.info("ScrapeViewSet.create: hold scrape id=%s url=%s", scrape.id, url)
            scr_ser = self.get_serializer()
            return Response(
                {"data": scr_ser.to_resource(scrape)},
                status=status.HTTP_201_CREATED,
            )

        # Check if scraping is enabled
        if not getattr(settings, "SCRAPING_ENABLED", False):
            logger.warning("ScrapeViewSet.create: SCRAPING_ENABLED=False, rejecting request")
            return Response(
                {"errors": [{"detail": "Scraping functionality is disabled"}]},
                status=status.HTTP_501_NOT_IMPLEMENTED,
            )

        if not url:
            logger.warning("ScrapeViewSet.create: missing url in request body")
            return Response(
                {"errors": [{"detail": "URL is required"}]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        logger.info("ScrapeViewSet.create url=%s", url)

        # Check for existing scrape with the same URL
        existing_scrape = Scrape.objects.filter(url=url).first()

        # If there's an existing scrape that's pending or completed, return it
        if existing_scrape:
            logger.info(
                "ScrapeViewSet.create: existing scrape found id=%s status=%s",
                existing_scrape.id,
                existing_scrape.status,
            )
            if existing_scrape.status in ("pending", "processing", "running"):
                # Return the existing pending/processing/running scrape
                scr_ser = self.get_serializer()
                scrape_resource = scr_ser.to_resource(existing_scrape)
                return Response(
                    {
                        "data": scrape_resource,
                        "meta": {"message": "Scrape already in progress for this URL"},
                    },
                    status=status.HTTP_200_OK,
                )
            elif existing_scrape.status == "completed":
                # Return the existing completed scrape
                scr_ser = self.get_serializer()
                scrape_resource = scr_ser.to_resource(existing_scrape)
                return Response(
                    {
                        "data": scrape_resource,
                        "meta": {
                            "message": "Scrape already completed for this URL. Use the redo action to re-scrape."
                        },
                    },
                    status=status.HTTP_200_OK,
                )
            # If failed, we'll create a new scrape below
            logger.info("ScrapeViewSet.create: existing scrape status=%s, creating new scrape", existing_scrape.status)

        source = attrs.get("source") or data.get("source") or "scrape"
        scrape = Scrape.objects.create(
            url=url,
            status="pending",
            created_by=request.user,
            source=source,
        )
        logger.info("ScrapeViewSet.create: created scrape id=%s", scrape.id)
        from job_hunting.lib.scraper import _log_scrape_status
        _log_scrape_status(scrape.id, "pending")

        # Associate with an existing job post (and its company) if the URL matches
        existing_jp = JobPost.objects.filter(link=url).first()
        if existing_jp:
            scrape.job_post = existing_jp
            scrape.company_id = existing_jp.company_id
            scrape.save()
            logger.info("ScrapeViewSet.create: linked scrape id=%s to job_post id=%s company_id=%s", scrape.id, existing_jp.id, existing_jp.company_id)

        browser_service_url = getattr(settings, "BROWSER_SERVICE_URL", "http://localhost:3012")
        logger.info("ScrapeViewSet.create: dispatching scraper browser_service_url=%s scrape_id=%s", browser_service_url, scrape.id)
        Scraper(browser_service_url, url, scrape_id=scrape.id).dispatch()

        scr_ser = self.get_serializer()
        scrape_resource = scr_ser.to_resource(scrape)
        return Response({"data": scrape_resource}, status=status.HTTP_202_ACCEPTED)

    @extend_schema(
        tags=["Scrapes"],
        summary="Re-scrape a URL (async — resets to pending)",
        responses={
            202: OpenApiResponse(description="Scrape restarted"),
            400: OpenApiResponse(description="Already pending/processing"),
            501: OpenApiResponse(description="Scraping disabled"),
        },
    )
    @action(detail=True, methods=["post"])
    def redo(self, request, pk=None):
        """Redo a scrape - resets status to pending and starts a new scrape process"""

        # Check if scraping is enabled
        if not getattr(settings, "SCRAPING_ENABLED", False):
            logger.warning("ScrapeViewSet.redo: SCRAPING_ENABLED=False, rejecting request")
            return Response(
                {"errors": [{"detail": "Scraping functionality is disabled"}]},
                status=status.HTTP_501_NOT_IMPLEMENTED,
            )

        obj = Scrape.objects.filter(pk=int(pk)).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        logger.info("ScrapeViewSet.redo: id=%s previous_status=%s url=%s", obj.id, obj.status, obj.url)

        # Don't allow redo if already pending or processing
        # if obj.status in ("pending", "processing"):
        #     return Response(
        #         {"errors": [{"detail": f"Scrape is already {obj.status}"}]},
        #         status=status.HTTP_400_BAD_REQUEST,
        #     )

        obj.status = "pending"
        obj.save()

        from job_hunting.lib.scraper import _log_scrape_status
        _log_scrape_status(obj.id, "pending", note="Redo requested")

        browser_service_url = getattr(settings, "BROWSER_SERVICE_URL", "http://localhost:3012")
        logger.info("ScrapeViewSet.redo: dispatching browser_service_url=%s scrape_id=%s", browser_service_url, obj.id)
        Scraper(browser_service_url, obj.url, scrape_id=obj.id).dispatch()

        # Return the updated scrape
        scr_ser = self.get_serializer()
        scrape_resource = scr_ser.to_resource(obj)
        return Response(
            {"data": scrape_resource, "meta": {"message": "Scrape restarted"}},
            status=status.HTTP_202_ACCEPTED,
        )

    @extend_schema(
        tags=["Scrapes"],
        summary="Parse scrape content into a JobPost and Company",
        responses={
            200: OpenApiResponse(description="Parsed successfully"),
            404: OpenApiResponse(description="Not found"),
            422: OpenApiResponse(description="No content to parse"),
        },
    )
    @action(detail=True, methods=["post"])
    def parse(self, request, pk=None):
        """Kick off parsing in a background thread. Returns 200 immediately."""
        obj = Scrape.objects.filter(pk=int(pk)).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)

        if not (obj.job_content or obj.html):
            return Response(
                {"errors": [{"detail": "Scrape has no content to parse"}]},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        logger.info("ScrapeViewSet.parse: id=%s force=True", obj.id)

        from job_hunting.lib.parsers.job_post_extractor import parse_scrape
        # User-initiated Parse always forces — even if a JobPost is already
        # linked, they've told us to refresh it from current scrape content.
        parse_scrape(obj.id, user_id=request.user.id, force=True)

        scr_ser = self.get_serializer()
        scrape_resource = scr_ser.to_resource(obj)
        return Response({"data": scrape_resource})

    @extend_schema(
        tags=["Scrapes"],
        summary="Create a Scrape from pasted text (skips browser fetch)",
        request=inline_serializer(
            name="ScrapeFromTextRequest",
            fields={
                "text": drf_serializers.CharField(
                    help_text="Raw job-post text pasted by the user"
                ),
                "link": drf_serializers.CharField(
                    required=False,
                    allow_blank=True,
                    help_text="Optional source URL for deduplication",
                ),
            },
        ),
        responses={
            202: OpenApiResponse(description="Parse dispatched"),
            400: OpenApiResponse(description="Empty text"),
        },
    )
    @action(detail=False, methods=["post"], url_path="from-text")
    def from_text(self, request):
        """Create a Scrape with job_content pre-filled from pasted text and
        kick off parse_scrape directly. No browser fetch, no hold-poller —
        status='pending' transitions through 'extracting' → terminal inside
        the daemon thread parse_scrape spawns.

        Short-circuits with 409 when `link` already maps to a non-stub
        JobPost — re-parsing a fully-extracted post wastes an LLM call
        and loses the user's existing data. Stubs (thin/empty
        description) still pass through so parse_scrape can upgrade
        them in place. Set `force=true` in the body to override.
        """
        data = request.data if isinstance(request.data, dict) else {}
        text = (data.get("text") or "").strip()
        link = (data.get("link") or "").strip() or None
        force = bool(data.get("force"))

        if not text:
            return Response(
                {"errors": [{"detail": "text is required"}]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if link and not force:
            existing = JobPost.objects.filter(link=link).first()
            if existing and not _is_thin_description(existing):
                return Response(
                    {
                        "errors": [
                            {
                                "status": "409",
                                "code": "duplicate_job_post",
                                "detail": (
                                    "A job post with this link already exists. "
                                    "Open it, or re-submit with force=true to re-parse."
                                ),
                                "meta": {
                                    "job_post_id": existing.id,
                                    "title": existing.title,
                                    "company_name": (
                                        existing.company.name
                                        if existing.company_id
                                        else None
                                    ),
                                    "link": existing.link,
                                },
                            }
                        ]
                    },
                    status=status.HTTP_409_CONFLICT,
                )

        scrape = Scrape.objects.create(
            url=link,
            job_content=text,
            status="pending",
            created_by=request.user,
            source="paste",
        )
        from job_hunting.lib.scraper import _log_scrape_status
        _log_scrape_status(scrape.id, "pending", note="paste ingest")

        from job_hunting.lib.parsers.job_post_extractor import parse_scrape
        parse_scrape(scrape.id, user_id=request.user.id)

        scr_ser = self.get_serializer()
        return Response(
            {"data": scr_ser.to_resource(scrape)},
            status=status.HTTP_202_ACCEPTED,
        )

    @action(detail=True, methods=["post"], url_path="llm-extract")
    def llm_extract(self, request, pk=None):
        """Run LLM extraction against a scrape's captured content and
        return the parsed ParsedJobData without persisting.

        Body: {"model": "<provider:model>"}

        Called by the ai-side scrape-graph's Tier1/2/3 nodes. Persistence
        is a separate call to persist-extraction — this endpoint is
        read-only on the scrape (apart from ai_usage accounting).
        """
        scrape = Scrape.objects.filter(pk=pk).first()
        if not scrape:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        if not request.user.is_staff and scrape.created_by_id != request.user.id:
            return Response({"errors": [{"detail": "Forbidden"}]}, status=403)

        body = request.data if isinstance(request.data, dict) else {}
        model_spec = (body.get("model") or "").strip() or None

        if not (scrape.job_content or scrape.html):
            return Response(
                {"errors": [{"detail": "Scrape has no captured content"}]},
                status=400,
            )

        from job_hunting.lib.parsers.job_post_extractor import JobPostExtractor

        try:
            parsed = JobPostExtractor().analyze_with_ai(scrape, model_override=model_spec)
        except Exception as exc:
            logger.exception("llm_extract failed for scrape %s", scrape.id)
            return Response(
                {"errors": [{"detail": f"Extraction failed: {exc}"}]},
                status=502,
            )

        attributes = parsed.model_dump(mode="json")
        return Response(
            {"data": {"type": "parsed-job-data", "attributes": attributes}},
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="persist-extraction")
    def persist_extraction(self, request, pk=None):
        """Accept an already-parsed ParsedJobData payload and run the
        server-side persistence half of parse_scrape (dedup-by-link +
        stub upgrade + posted_date fallback + JobPost create/update).

        Body: {"data": {"attributes": {"title", "company_name",
        "company_display_name?", "description?", "posted_date?",
        "extraction_date?", "salary_min?", "salary_max?", "location?",
        "remote?", "link?"}, "force?": bool}}

        Called by the ai-side scrape-graph's PersistJobPost node. The
        legacy parse_scrape still exists and still works — this just
        lets ai/ do the extraction itself (pydantic-ai in ai/) and
        hand the result to api/ for persistence.
        """
        scrape = Scrape.objects.filter(pk=pk).first()
        if not scrape:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        # Ownership: the scrape's owner OR staff.
        if not request.user.is_staff and scrape.created_by_id != request.user.id:
            return Response({"errors": [{"detail": "Forbidden"}]}, status=403)

        body = request.data if isinstance(request.data, dict) else {}
        attrs = (body.get("data") or {}).get("attributes") or body.get("attributes") or {}
        force = bool(body.get("force") or attrs.get("force"))

        from job_hunting.lib.parsers.job_post_extractor import (
            JobPostExtractor,
            ParsedJobData,
        )

        try:
            validated = ParsedJobData(**{
                k: v for k, v in attrs.items()
                if k in ParsedJobData.model_fields
            })
        except Exception as exc:
            return Response(
                {"errors": [{"detail": f"Invalid ParsedJobData: {exc}"}]},
                status=400,
            )

        extractor = JobPostExtractor()
        user = scrape.created_by
        ok = extractor.process_evaluation(scrape, validated, user=user, force=force)
        scrape.refresh_from_db()
        ser = self.get_serializer()
        return Response(
            {
                "data": ser.to_resource(scrape),
                "meta": {
                    "persisted": ok,
                    "outcome": getattr(extractor, "last_outcome", None),
                    "job_post_id": scrape.job_post_id,
                },
            },
            status=status.HTTP_200_OK if ok else status.HTTP_400_BAD_REQUEST,
        )

    @action(detail=True, methods=["patch", "post"], url_path="apply-url")
    def apply_url(self, request, pk=None):
        """Persist the apply-destination resolver outcome.

        Body: {"data": {"attributes": {"apply_url?": "https://...",
        "apply_url_status": "resolved|internal|failed|stale|unknown",
        "apply_candidates?": [{"selector": "...", "href": "...",
                                "text": "...", "tag": "...",
                                "score": 0.0, "reason": "..."}]}}}

        Writes the result to this Scrape AND through to its JobPost
        (if linked), stamping `apply_url_resolved_at` on the JobPost when
        the status is `resolved` or `internal`. Called by the
        ResolveApplyUrl node in the scrape-graph.

        `apply_candidates` is the Phase 3 learning-loop capture: when
        the resolver ends in unknown/failed, the heuristic scan stores
        candidate "Apply" elements here for later aggregation. Capped
        at 50 candidates to keep payloads bounded — should be plenty
        for any single page.
        """
        scrape = Scrape.objects.filter(pk=pk).first()
        if not scrape:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        if not request.user.is_staff and scrape.created_by_id != request.user.id:
            return Response({"errors": [{"detail": "Forbidden"}]}, status=403)

        body = request.data if isinstance(request.data, dict) else {}
        attrs = (body.get("data") or {}).get("attributes") or body.get("attributes") or body

        new_status = (attrs.get("apply_url_status") or "").strip()
        valid = {"unknown", "resolved", "internal", "failed", "stale"}
        if new_status not in valid:
            return Response(
                {"errors": [{"detail": f"apply_url_status must be one of {sorted(valid)}"}]},
                status=400,
            )
        new_url = attrs.get("apply_url") or None
        if new_url and len(new_url) > 2000:
            return Response(
                {"errors": [{"detail": "apply_url too long (max 2000)"}]},
                status=400,
            )

        new_candidates = attrs.get("apply_candidates")
        if new_candidates is not None:
            if not isinstance(new_candidates, list):
                return Response(
                    {"errors": [{"detail": "apply_candidates must be a list"}]},
                    status=400,
                )
            new_candidates = new_candidates[:50]

        from django.utils import timezone

        scrape.apply_url = new_url
        scrape.apply_url_status = new_status
        update_fields = ["apply_url", "apply_url_status"]
        if new_candidates is not None:
            scrape.apply_candidates = new_candidates
            update_fields.append("apply_candidates")
        scrape.save(update_fields=update_fields)

        job_post = scrape.job_post
        if job_post is not None:
            job_post.apply_url = new_url
            job_post.apply_url_status = new_status
            if new_status in ("resolved", "internal"):
                job_post.apply_url_resolved_at = timezone.now()
            job_post.save(update_fields=[
                "apply_url", "apply_url_status", "apply_url_resolved_at",
            ])

        scrape.refresh_from_db()
        ser = self.get_serializer()
        return Response(
            {
                "data": ser.to_resource(scrape),
                "meta": {
                    "job_post_id": scrape.job_post_id,
                    "apply_url": scrape.apply_url,
                    "apply_url_status": scrape.apply_url_status,
                },
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="graph-transition")
    def graph_transition(self, request, pk=None):
        """Record a scrape-graph node transition as a ScrapeStatus row.

        Body: {"graph_node": "Tier1Mini", "graph_payload": {...},
        "note?": "free-form", "status?": "extracting"}

        `status` is optional; when present the api resolves the Status
        row by name (same lookup parse_scrape uses) and attaches it.
        Called by the BaseNode tracing mixin from ai/ after every
        node transition.
        """
        scrape = Scrape.objects.filter(pk=pk).first()
        if not scrape:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        if not request.user.is_staff and scrape.created_by_id != request.user.id:
            return Response({"errors": [{"detail": "Forbidden"}]}, status=403)

        body = request.data if isinstance(request.data, dict) else {}
        graph_node = body.get("graph_node")
        graph_payload = body.get("graph_payload") or {}
        note = body.get("note")
        status_name = body.get("status")
        if not graph_node:
            return Response(
                {"errors": [{"detail": "graph_node is required"}]},
                status=400,
            )

        from job_hunting.lib.scraper import _log_scrape_status
        # Never update Scrape.status from a graph transition — per-node
        # trace entries ('tier1mini', 'resolveapplyurl', ...) would mask
        # the legacy terminal status the poller polls on, leaving UI
        # spinners running long after the scrape itself completed.
        # Caller can still force the update by passing an explicit
        # `status` (used by legacy bridges, not the graph runner).
        _log_scrape_status(
            scrape.id,
            status_name or graph_node.lower(),
            note=note,
            graph_node=graph_node,
            graph_payload=graph_payload,
            update_scrape_status=bool(status_name),
        )
        return Response({"data": {"recorded": True}}, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"], url_path="graph-trace")
    def graph_trace(self, request, pk=None):
        """Ordered ScrapeStatus rows with graph fields for the force-
        layout UI. Includes the source_scrape chain so a tracker URL
        and its canonical child render as one path."""
        scrape = Scrape.objects.filter(pk=pk).first()
        if not scrape:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        if not request.user.is_staff and scrape.created_by_id != request.user.id:
            return Response({"errors": [{"detail": "Forbidden"}]}, status=403)

        from job_hunting.models.scrape_status import ScrapeStatus
        # Walk up source_scrape chain to the root so the trace includes
        # the whole redirect story.
        chain = [scrape]
        cursor = scrape.source_scrape
        while cursor and cursor.id not in {s.id for s in chain}:
            chain.insert(0, cursor)
            cursor = cursor.source_scrape
        ids = [s.id for s in chain]
        rows = list(
            ScrapeStatus.objects
            .filter(scrape_id__in=ids, graph_node__isnull=False)
            .order_by("scrape_id", "created_at", "id")
        )
        data = [
            {
                "scrape_id": r.scrape_id,
                "graph_node": r.graph_node,
                "graph_payload": r.graph_payload,
                "note": r.note,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
        return Response({
            "data": data,
            "meta": {
                "chain": [{"id": s.id, "url": s.url, "source": s.source} for s in chain],
            },
        })

    @action(detail=True, methods=["get", "post"], url_path="screenshots")
    def screenshots(self, request, pk=None):
        """GET: list screenshot filenames. POST: upload a screenshot PNG."""
        if request.method == "POST":
            uploaded = request.FILES.get("file")
            if not uploaded:
                return Response(
                    {"errors": [{"detail": "No file provided"}]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            from job_hunting.lib.screenshot_store import ScreenshotStore
            store = ScreenshotStore(settings.SCREENSHOT_DIR)
            store.save(int(pk), uploaded.name, uploaded)
            return Response({"data": {"filename": uploaded.name}}, status=status.HTTP_201_CREATED)

        if not request.user.is_staff:
            return Response(
                {"errors": [{"detail": "Staff access required"}]},
                status=status.HTTP_403_FORBIDDEN,
            )
        from job_hunting.lib.screenshot_store import ScreenshotStore
        store = ScreenshotStore(settings.SCREENSHOT_DIR)
        files = store.list_for_scrape(int(pk))
        resp = Response({"data": files})
        # The poller writes screenshots mid-lifecycle; any cached empty list
        # would persist until the user hard-refreshes. Force revalidation
        # by telling browsers/proxies not to store this response.
        resp["Cache-Control"] = "no-store"
        return resp

    @action(
        detail=True,
        methods=["get"],
        url_path="screenshots/(?P<filename>[^/]+)",
        url_name="screenshot-file",
    )
    def screenshot_file(self, request, pk=None, filename=None, **kwargs):
        """Serve a screenshot PNG. Staff only."""
        if not request.user.is_staff:
            return Response(
                {"errors": [{"detail": "Staff access required"}]},
                status=status.HTTP_403_FORBIDDEN,
            )
        from django.http import FileResponse
        from job_hunting.lib.screenshot_store import ScreenshotStore
        store = ScreenshotStore(settings.SCREENSHOT_DIR)
        path = store.read(int(pk), filename)
        if not path:
            return Response(
                {"errors": [{"detail": "Screenshot not found"}]},
                status=status.HTTP_404_NOT_FOUND,
            )
        return FileResponse(open(path, "rb"), content_type="image/png")

    @action(detail=True, methods=["get"], url_path="scrape-statuses")
    def scrape_statuses(self, request, pk=None):
        """List status history for a scrape."""
        from job_hunting.models.scrape_status import ScrapeStatus
        qs = ScrapeStatus.objects.filter(scrape_id=int(pk)).select_related("status").order_by("logged_at")
        data = []
        for ss in qs:
            data.append({
                "type": "scrape-status",
                "id": str(ss.id),
                "attributes": {
                    "logged_at": ss.logged_at.isoformat() if ss.logged_at else None,
                    "note": ss.note,
                    "created_at": ss.created_at.isoformat() if ss.created_at else None,
                    "graph_node": ss.graph_node,
                    "graph_payload": ss.graph_payload,
                },
                "relationships": {
                    "scrape": {"data": {"type": "scrape", "id": str(ss.scrape_id)}},
                    "status": {"data": {"type": "status", "id": str(ss.status_id)} if ss.status_id else None},
                },
            })
        # Include the status records so Ember Data can resolve them
        included = []
        seen = set()
        for ss in qs:
            if ss.status_id and ss.status_id not in seen:
                seen.add(ss.status_id)
                included.append({
                    "type": "status",
                    "id": str(ss.status_id),
                    "attributes": {
                        "status": ss.status.status,
                        "status_type": ss.status.status_type,
                    },
                })
        payload = {"data": data}
        if included:
            payload["included"] = included
        return Response(payload)


class ScrapeProfileViewSet(BaseViewSet):
    model = ScrapeProfile
    serializer_class = ScrapeProfileSerializer
    permission_classes = [IsAuthenticated, IsAdminUser]

    def list(self, request):
        qs = ScrapeProfile.objects.all().order_by("-scrape_count")
        hostname = request.query_params.get("filter[hostname]")
        if hostname:
            qs = qs.filter(hostname=hostname)
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
        obj = ScrapeProfile.objects.filter(pk=pk).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer()
        return Response({"data": ser.to_resource(obj)})

    def partial_update(self, request, pk=None):
        obj = ScrapeProfile.objects.filter(pk=pk).first()
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = request.data if isinstance(request.data, dict) else {}
        node = data.get("data") or {}
        attrs = node.get("attributes") or {}
        editable = [
            "extraction_hints", "page_structure", "css_selectors",
            "preferred_tier", "enabled",
        ]
        for field in editable:
            json_key = field.replace("_", "-")
            if json_key in attrs:
                setattr(obj, field, attrs[json_key])
            elif field in attrs:
                setattr(obj, field, attrs[field])
        obj.save()
        ser = self.get_serializer()
        return Response({"data": ser.to_resource(obj)})

    @action(
        detail=False,
        methods=["post"],
        url_path=r"(?P<hostname>[^/]+)/update-from-outcome",
    )
    def update_from_outcome(self, request, hostname=None):
        """Record a scrape outcome against a ScrapeProfile — wraps
        _update_scrape_profile so the ai-side graph's UpdateProfile
        node can bump success rate / tier0 miss counters / auto-demote
        without Django ORM access.

        Body: {"success": bool, "tier0_hit": bool | null}
        """
        from job_hunting.lib.parsers.job_post_extractor import (
            _update_scrape_profile,
        )
        from job_hunting.models import Scrape

        body = request.data if isinstance(request.data, dict) else {}
        scrape_id = body.get("scrape_id")
        if not scrape_id:
            return Response(
                {"errors": [{"detail": "scrape_id is required"}]}, status=400,
            )
        scrape = Scrape.objects.filter(pk=scrape_id).first()
        if not scrape:
            return Response(
                {"errors": [{"detail": "scrape not found"}]}, status=404,
            )
        success = bool(body.get("success", False))
        tier0_hit = body.get("tier0_hit", None)
        if tier0_hit is not None:
            tier0_hit = bool(tier0_hit)
        _update_scrape_profile(
            scrape, scrape.created_by, success=success, tier0_hit=tier0_hit,
        )
        return Response({"data": {"recorded": True, "hostname": hostname}})

