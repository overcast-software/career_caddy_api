import logging
import math

from django.db.models import Q
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

        # Sorting
        sort_param = request.query_params.get("sort")
        if sort_param:
            sort_fields = []
            for field in sort_param.split(","):
                field = field.strip()
                if field.startswith("-"):
                    sort_fields.append(f"-{field[1:]}")
                else:
                    sort_fields.append(field)
            if sort_fields:
                qs = qs.order_by(*sort_fields)
        else:
            # Default: latest first
            try:
                Scrape._meta.get_field("created_at")
                qs = qs.order_by("-created_at")
            except Exception:
                qs = qs.order_by("-id")

        # Status filter
        status_filter = request.query_params.get("filter[status]")
        if status_filter:
            qs = qs.filter(status=status_filter)

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

    def _maybe_trigger_extraction(self, pk):
        from job_hunting.lib.scraper import _maybe_caddy_extract
        scrape = Scrape.objects.filter(pk=int(pk)).first()
        if scrape:
            _maybe_caddy_extract(scrape)

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
        self._maybe_trigger_extraction(pk)
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
        self._maybe_trigger_extraction(pk)
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
            scrape = Scrape.objects.create(url=url, status="hold", created_by=request.user)
            from job_hunting.lib.scraper import _log_scrape_status
            _log_scrape_status(scrape.id, "hold")
            # Link to existing job post if URL matches
            existing_jp = JobPost.objects.filter(link=url).first()
            if existing_jp:
                scrape.job_post = existing_jp
                scrape.company_id = existing_jp.company_id
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

        scrape = Scrape.objects.create(url=url, status="pending", created_by=request.user)
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

        logger.info("ScrapeViewSet.parse: id=%s", obj.id)

        from job_hunting.lib.parsers.job_post_extractor import parse_scrape
        parse_scrape(obj.id, user_id=request.user.id)

        scr_ser = self.get_serializer()
        scrape_resource = scr_ser.to_resource(obj)
        return Response({"data": scrape_resource})

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
        return Response({"data": files})

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

