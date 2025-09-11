import asyncio
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from job_hunting.lib.models import (
    User,
    Resume,
    Score,
    JobPost,
    Scrape,
    Company,
    CoverLetter,
    Application,
)
from .serializers import (
    UserSerializer,
    ResumeSerializer,
    ScoreSerializer,
    JobPostSerializer,
    ScrapeSerializer,
    CompanySerializer,
    CoverLetterSerializer,
    ApplicationSerializer,
    TYPE_TO_SERIALIZER,
)


class BaseSAViewSet(viewsets.ViewSet):
    permission_classes = [AllowAny]
    model = None
    serializer_class = None

    def get_session(self):
        return self.model.get_session()

    def get_serializer(self):
        return self.serializer_class()

    def _parse_include(self, request):
        inc = request.query_params.get("include")
        if inc:
            return [s.strip() for s in inc.split(",") if s.strip()]
        # Default: include all first-level relationships for this resource
        ser = self.get_serializer()
        return list(getattr(ser, "relationships", {}).keys())

    def _build_included(self, objs, include_rels):
        included = []
        seen = set()  # (type, id) for de-duplication
        primary_ser = self.get_serializer()
        for obj in objs:
            for rel in include_rels:
                rel_type, targets = primary_ser.get_related(obj, rel)
                if not rel_type:
                    continue
                ser_cls = TYPE_TO_SERIALIZER.get(rel_type)
                if not ser_cls:
                    continue
                rel_ser = ser_cls()
                for t in targets:
                    key = (rel_type, str(t.id))
                    if key in seen:
                        continue
                    seen.add(key)
                    included.append(rel_ser.to_resource(t))
        return included

    def paginate(self, items):
        try:
            page_number = int(self.request.query_params.get("page[number]", 1))
        except Exception:
            page_number = 1
        try:
            page_size = int(self.request.query_params.get("page[size]", 50))
        except Exception:
            page_size = 50
        start = (page_number - 1) * page_size
        end = start + page_size
        return items[start:end]

    def list(self, request):
        session = self.get_session()
        items = session.query(self.model).all()
        items = self.paginate(items)
        ser = self.get_serializer()
        data = [ser.to_resource(o) for o in items]
        payload = {"data": data}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included(items, include_rels)
        return Response(payload)

    def retrieve(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer()
        payload = {"data": ser.to_resource(obj)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([obj], include_rels)
        return Response(payload)

    def create(self, request):
        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response({"errors": [{"detail": str(e)}]}, status=400)
        obj = self.model(**attrs)
        session = self.get_session()
        session.add(obj)
        session.commit()
        return Response({"data": ser.to_resource(obj)}, status=status.HTTP_201_CREATED)

    def update(self, request, pk=None):
        return self._upsert(request, pk, partial=False)

    def partial_update(self, request, pk=None):
        return self._upsert(request, pk, partial=True)

    def _upsert(self, request, pk, partial=False):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer()
        try:
            attrs = ser.parse_payload(request.data)
        except ValueError as e:
            return Response({"errors": [{"detail": str(e)}]}, status=400)
        for k, v in attrs.items():
            setattr(obj, k, v)
        session = self.get_session()
        session.add(obj)
        session.commit()
        return Response({"data": ser.to_resource(obj)})

    def destroy(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response(status=204)
        session = self.get_session()
        session.delete(obj)
        session.commit()
        return Response(status=204)

    # JSON:API relationships linkage endpoint:
    # GET /<type>/{id}/relationships/<rel-name>
    @action(detail=True, methods=["get"], url_path=r"relationships/(?P<rel>[^/]+)")
    def relationships(self, request, pk=None, rel=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = self.get_serializer()
        cfg = (
            ser.relationships.get(rel)
            if ser and hasattr(ser, "relationships")
            else None
        )
        if not cfg:
            return Response(
                {"errors": [{"detail": "Relationship not found"}]}, status=404
            )
        rel_type = cfg["type"]
        uselist = cfg.get("uselist", True)
        target = getattr(obj, cfg["attr"], None)
        if uselist:
            data = [{"type": rel_type, "id": str(i.id)} for i in (target or [])]
        else:
            data = {"type": rel_type, "id": str(target.id)} if target else None
        return Response({"data": data})


class UserViewSet(BaseSAViewSet):
    model = User
    serializer_class = UserSerializer

    @action(detail=True, methods=["get"])
    def resumes(self, request, pk=None):
        user = self.model.get(int(pk))
        if not user:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [ResumeSerializer().to_resource(r) for r in (user.resumes or [])]
        return Response({"data": data})

    @action(detail=True, methods=["get"])
    def scores(self, request, pk=None):
        user = self.model.get(int(pk))
        if not user:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [ScoreSerializer().to_resource(s) for s in (user.scores or [])]
        return Response({"data": data})

    @action(detail=True, methods=["get"], url_path="cover-letters")
    def cover_letters(self, request, pk=None):
        user = self.model.get(int(pk))
        if not user:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [
            CoverLetterSerializer().to_resource(c) for c in (user.cover_letters or [])
        ]
        return Response({"data": data})

    @action(detail=True, methods=["get"])
    def applications(self, request, pk=None):
        user = self.model.get(int(pk))
        if not user:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [
            ApplicationSerializer().to_resource(a) for a in (user.applications or [])
        ]
        return Response({"data": data})


class ResumeViewSet(BaseSAViewSet):
    model = Resume
    serializer_class = ResumeSerializer

    @action(detail=True, methods=["get"])
    def scores(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [ScoreSerializer().to_resource(s) for s in (obj.scores or [])]
        return Response({"data": data})

    @action(detail=True, methods=["get"], url_path="cover-letters")
    def cover_letters(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [
            CoverLetterSerializer().to_resource(c) for c in (obj.cover_letters or [])
        ]
        return Response({"data": data})

    @action(detail=True, methods=["get"])
    def applications(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [
            ApplicationSerializer().to_resource(a) for a in (obj.applications or [])
        ]
        return Response({"data": data})


class ScoreViewSet(BaseSAViewSet):
    model = Score
    serializer_class = ScoreSerializer


class JobPostViewSet(BaseSAViewSet):
    model = JobPost
    serializer_class = JobPostSerializer

    def create(self, request):
        # Detect a "url" key in either a plain JSON body or JSON:API attributes
        data = request.data if isinstance(request.data, dict) else {}
        url = data.get("url")
        if url is None and isinstance(data.get("data"), dict):
            url = (data["data"].get("attributes") or {}).get("url")

        # If no URL provided, fall back to the default JSON:API create
        if not url:
            return super().create(request)

        # URL present: alternate path (kick off scrape/parse pipeline)

        # Lazy import to avoid hard dependency at module import time
        from job_hunting.lib.ai_client import ai_client
        from job_hunting.lib.browser_manager import BrowserManager
        # Lazy import to avoid heavy deps at module import time
        from job_hunting.lib.services.generic_service import GenericService

        browser_manager = BrowserManager()
        service = GenericService(
            url=url, browser=browser_manager, ai_client=ai_client, creds={}
        )
        try:
            try:
                scrape = asyncio.run(service.process())
                asyncio.run(browser_manager.close_browser())
            except RuntimeError:
                # If an event loop is already running (e.g., under ASGI), use it
                loop = asyncio.get_event_loop()
                scrape = loop.run_until_complete(service.process())
                loop.run_until_complete(browser_manager.close_browser())
        except Exception as e:
            return Response(
                {"errors": [{"detail": f"Failed to process URL: {e}"}]},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # Try to resolve the created/linked JobPost; if not available, return 202 with the scrape
        session = self.get_session()
        job_post = None
        try:
            if getattr(scrape, "job_post_id", None):
                job_post = JobPost.get(scrape.job_post_id, session=session)
            else:
                job_post = JobPost.first(session=session, url=url)
        except Exception:
            job_post = None

        if job_post:
            serializer = self.get_serializer()
            resource = serializer.to_resource(job_post)
            include_rels = self._parse_include(request)
            payload = {"data": resource}
            if include_rels:
                payload["included"] = self._build_included([job_post], include_rels)
            return Response(payload, status=status.HTTP_201_CREATED)

        # Could not resolve a JobPost yet — return the Scrape so the client can track progress
        ScrapeSerializer = TYPE_TO_SERIALIZER["scrape"]
        scr_ser = ScrapeSerializer()
        scrape_resource = scr_ser.to_resource(scrape)
        return Response({"data": scrape_resource}, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=["get"])
    def scores(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [ScoreSerializer().to_resource(s) for s in (obj.scores or [])]
        return Response({"data": data})

    @action(detail=True, methods=["get"])
    def scrapes(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [ScrapeSerializer().to_resource(s) for s in (obj.scrapes or [])]
        return Response({"data": data})

    @action(detail=True, methods=["get"], url_path="cover-letters")
    def cover_letters(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [
            CoverLetterSerializer().to_resource(c) for c in (obj.cover_letters or [])
        ]
        return Response({"data": data})

    @action(detail=True, methods=["get"])
    def applications(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [
            ApplicationSerializer().to_resource(a) for a in (obj.applications or [])
        ]
        return Response({"data": data})


class ScrapeViewSet(BaseSAViewSet):
    model = Scrape
    serializer_class = ScrapeSerializer


class CompanyViewSet(BaseSAViewSet):
    model = Company
    serializer_class = CompanySerializer

    @action(detail=True, methods=["get"], url_path="job-posts")
    def job_posts(self, request, pk=None):
        company = self.model.get(int(pk))
        if not company:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [JobPostSerializer().to_resource(j) for j in (company.job_posts or [])]
        return Response({"data": data})

    @action(detail=True, methods=["get"])
    def scrapes(self, request, pk=None):
        company = self.model.get(int(pk))
        if not company:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [ScrapeSerializer().to_resource(s) for s in (company.scrapes or [])]
        return Response({"data": data})


class CoverLetterViewSet(BaseSAViewSet):
    model = CoverLetter
    serializer_class = CoverLetterSerializer


class ApplicationViewSet(BaseSAViewSet):
    model = Application
    serializer_class = ApplicationSerializer
