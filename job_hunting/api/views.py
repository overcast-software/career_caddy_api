import asyncio
import re
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.parsers import JSONParser
from .parsers import VndApiJSONParser
from job_hunting.lib.scoring.job_scorer import JobScorer
from job_hunting.lib.ai_client import ai_client
from job_hunting.lib.services.summary_service import SummaryService
from job_hunting.lib.services.cover_letter_service import CoverLetterService
from job_hunting.lib.services.generic_service import GenericService

from job_hunting.lib.models import (
    User,
    Resume,
    Score,
    JobPost,
    Scrape,
    Company,
    CoverLetter,
    Application,
    Summary,
    Experience,
    Education,
    Certification,
    Description,
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
    SummarySerializer,
    ExperienceSerializer,
    EducationSerializer,
    CertificationSerializer,
    DescriptionSerializer,
    TYPE_TO_SERIALIZER,
)


class BaseSAViewSet(viewsets.ViewSet):
    permission_classes = [AllowAny]
    parser_classes = [VndApiJSONParser, JSONParser]
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
                # Provide parent context so serializers can customize included resources
                if hasattr(rel_ser, "set_parent_context"):
                    rel_ser.set_parent_context(primary_ser.type, obj.id, rel)
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


class SummaryViewSet(BaseSAViewSet):
    model = Summary
    serializer_class = SummarySerializer

    def create(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        node = (data.get("data") or {})
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
        job_post_id = _rel_id("job-post", "job_post", "jobPost", "job-posts", "jobPosts")
        user_id = _rel_id("user", "users")

        if resume_id is None:
            return Response(
                {"errors": [{"detail": "Missing required relationship: resume"}]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            resume = Resume.get(int(resume_id))
        except (TypeError, ValueError):
            resume = None
        if not resume:
            return Response(
                {"errors": [{"detail": "Invalid resume ID"}]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        job_post = None
        if job_post_id is not None:
            try:
                job_post = JobPost.get(int(job_post_id))
            except (TypeError, ValueError):
                job_post = None
            if not job_post:
                return Response(
                    {"errors": [{"detail": "Invalid job-post ID"}]},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Resolve user_id; default to resume.user_id if not provided or invalid
        try:
            user_id = int(user_id) if user_id is not None else None
        except (TypeError, ValueError):
            user_id = None
        if user_id is None:
            user_id = getattr(resume, "user_id", None)

        content = attrs.get("content")

        if content:
            summary = Summary(
                resume_id=resume.id,
                job_post_id=job_post.id if job_post else None,
                user_id=user_id,
                content=content,
            )
            summary.save()
        else:
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
            summary_service = SummaryService(ai_client, job=job_post, resume=resume)
            summary = summary_service.generate_summary()

        ser = self.get_serializer()
        payload = {"data": ser.to_resource(summary)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([summary], include_rels)
        return Response(payload, status=status.HTTP_201_CREATED)


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

    @action(detail=True, methods=["get"])
    def summaries(self, request, pk=None):
        user = self.model.get(int(pk))
        if not user:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [SummarySerializer().to_resource(s) for s in (user.summaries or [])]
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

    @action(detail=True, methods=["get", "post"])
    def summaries(self, request, pk=None):
        if request.method.lower() == "post":
            obj = self.model.get(int(pk))  # obj is the Resume
            if not obj:
                return Response({"errors": [{"detail": "Not found"}]}, status=404)

            data = request.data if isinstance(request.data, dict) else {}
            node = (data.get("data") or {})
            attrs = node.get("attributes") or {}
            relationships = node.get("relationships") or {}

            # Accept hyphenated or underscored job-post relationship; allow null if content is provided
            job_post_rel = (
                relationships.get("job-post")
                or relationships.get("job_post")
                or relationships.get("jobPost")
                or relationships.get("job-posts")
                or relationships.get("jobPosts")
                or {}
            )
            job_post_id = None
            if isinstance(job_post_rel, dict):
                d = job_post_rel.get("data")
                if isinstance(d, dict):
                    job_post_id = d.get("id")

            job_post = None
            if job_post_id is not None:
                try:
                    job_post = JobPost.get(int(job_post_id))
                except (TypeError, ValueError):
                    job_post = None
                if not job_post:
                    return Response(
                        {"errors": [{"detail": "Invalid job-post ID"}]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            content = attrs.get("content")
            if content:
                summary = Summary(
                    resume_id=obj.id,
                    job_post_id=job_post.id if job_post else None,
                    user_id=getattr(obj, "user_id", None),
                    content=content,
                )
                summary.save()
            else:
                if not job_post:
                    return Response(
                        {"errors": [{"detail": "Provide 'attributes.content' or a job-post relationship to generate content"}]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                summary_service = SummaryService(ai_client, job=job_post, resume=obj)
                summary = summary_service.generate_summary()

            ser = SummarySerializer()
            payload = {"data": ser.to_resource(summary)}
            include_rels = self._parse_include(request)
            if include_rels:
                payload["included"] = self._build_included([summary], include_rels)
            return Response(payload, status=status.HTTP_201_CREATED)

        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [SummarySerializer().to_resource(s) for s in (obj.summaries or [])]
        return Response({"data": data})

    @action(detail=True, methods=["get"])
    def experiences(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = ExperienceSerializer()
        ser.set_parent_context("resume", obj.id, "experiences")
        items = list(obj.experiences or [])
        data = [ser.to_resource(e) for e in items]

        # Build included using ExperienceSerializer so descriptions (and other first-level rels) are returned
        inc_param = request.query_params.get("include")
        if inc_param:
            include_rels = [s.strip() for s in inc_param.split(",") if s.strip()]
        else:
            include_rels = list(getattr(ser, "relationships", {}).keys())

        included = []
        if include_rels:
            seen = set()
            for exp in items:
                for rel in include_rels:
                    rel_type, targets = ser.get_related(exp, rel)
                    if not rel_type:
                        continue
                    ser_cls = TYPE_TO_SERIALIZER.get(rel_type)
                    if not ser_cls:
                        continue
                    rel_ser = ser_cls()
                    if hasattr(rel_ser, "set_parent_context"):
                        rel_ser.set_parent_context(ser.type, exp.id, rel)
                    for t in targets:
                        key = (rel_type, str(t.id))
                        if key in seen:
                            continue
                        seen.add(key)
                        included.append(rel_ser.to_resource(t))

        payload = {"data": data}
        if included:
            payload["included"] = included
        return Response(payload)

    @action(detail=True, methods=["get"])
    def educations(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        ser = EducationSerializer()
        ser.set_parent_context("resume", obj.id, "educations")
        data = [ser.to_resource(e) for e in (obj.educations or [])]
        return Response({"data": data})


class ScoreViewSet(BaseSAViewSet):
    model = Score
    serializer_class = ScoreSerializer

    def create(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        myJobScorer = JobScorer(ai_client)

        relationships = (data.get("data") or {}).get("relationships") or {}

        def _first_id(node):
            if isinstance(node, dict):
                data = node.get("data")
            else:
                data = None
            if isinstance(data, dict) and "id" in data:
                return data["id"]
            if isinstance(data, list) and data:
                first = data[0]
                if isinstance(first, dict) and "id" in first:
                    return first["id"]
            return None

        def _rel_id(*keys):
            for k in keys:
                val = relationships.get(k)
                if val is not None:
                    rid = _first_id(val)
                    if rid is not None:
                        return rid
            return None

        job_post_id = _rel_id("job-post", "job_post", "jobPost", "job-posts", "jobPosts")
        user_id = _rel_id("user", "users")
        resume_id = _rel_id("resume", "resumes")

        if job_post_id is None or user_id is None or resume_id is None:
            return Response(
                {"errors": [{"detail": "Missing required relationships: user, job-post, resume"}]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        job_post_id = int(job_post_id)
        user_id = int(user_id)
        resume_id = int(resume_id)

        jp = JobPost.get(job_post_id)
        resume = Resume.get(resume_id)

        myScore, is_created = Score.first_or_initialize(
            job_post_id=job_post_id, resume_id=resume_id, user_id=user_id
        )

        evaluation = myJobScorer.score_job_match(jp.description, resume.content)
        score_value, explanation = self._parse_eval(evaluation)
        myScore.explanation = explanation
        myScore.score = score_value
        myScore.save()

        ser = self.get_serializer()
        payload = {"data": ser.to_resource(myScore)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([myScore], include_rels)
        return Response(payload, status=status.HTTP_201_CREATED)

    def _parse_eval(self, e):
        if isinstance(e, dict):
            s = e.get("score")
            expl = e.get("explanation") or e.get("evaluation")
            if isinstance(expl, dict):
                expl = expl.get("text") or str(expl)
            if s is not None and expl:
                return int(s), str(expl)
        text = str(e)
        m_score = re.search(r"(?i)\**score\**\s*[:\-]\s*(\d{1,3})", text)
        m_expl = re.search(
            r"(?i)\**(explanation|evaluation)\**\s*[:\-]\s*(.+)", text, re.DOTALL
        )
        s_val = int(m_score.group(1)) if m_score else None
        expl = m_expl.group(2).strip() if m_expl else text
        return s_val, expl


class JobPostViewSet(BaseSAViewSet):
    model = JobPost
    serializer_class = JobPostSerializer

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

    @action(detail=True, methods=["get", "post"])
    def summaries(self, request, pk=None):
        if request.method.lower() == "post":
            obj = self.model.get(int(pk))  # obj is the JobPost
            if not obj:
                return Response({"errors": [{"detail": "Not found"}]}, status=404)

            data = request.data if isinstance(request.data, dict) else {}
            node = (data.get("data") or {})
            attrs = node.get("attributes") or {}
            relationships = node.get("relationships") or {}

            # Accept "resume"/"resumes" for resume relationship
            resume_rel = relationships.get("resumes") or relationships.get("resume") or {}
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
                resume = Resume.get(int(resume_id))
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
                    resume_id=resume.id,
                    job_post_id=obj.id,
                    user_id=getattr(resume, "user_id", None),
                    content=content,
                )
                summary.save()
            else:
                summary_service = SummaryService(ai_client, job=obj, resume=resume)
                summary = summary_service.generate_summary()

            ser = SummarySerializer()
            payload = {"data": ser.to_resource(summary)}
            include_rels = self._parse_include(request)
            if include_rels:
                payload["included"] = self._build_included([summary], include_rels)
            return Response(payload, status=status.HTTP_201_CREATED)

        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [SummarySerializer().to_resource(s) for s in (obj.summaries or [])]
        return Response({"data": data})


class ScrapeViewSet(BaseSAViewSet):
    model = Scrape
    serializer_class = ScrapeSerializer

    def create(self, request):
        # Detect a "url" key in either a plain JSON body or JSON:API attributes
        data = request.data if isinstance(request.data, dict) else {}
        url = data.get("url")
        if url is None and isinstance(data.get("data"), dict):
            url = (data["data"].get("attributes") or {}).get("url")

        # Lazy import to avoid hard dependency at module import time
        from job_hunting.lib.browser_manager import BrowserManager

        # Lazy import to avoid heavy deps at module import time

        browser_manager = BrowserManager()
        asyncio.run(browser_manager.start_browser(False))
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
            job_post = scrape.job_post
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

    def create(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        relationships = (data.get("data") or {}).get("relationships") or {}
        job_post_id = relationships.get("job-post", {}).get("data", {}).get("id")
        # Support both "resume" and "resumes" relationship keys
        resume_rel = relationships.get("resumes") or relationships.get("resume") or {}
        resume_id = resume_rel.get("data", {}).get("id")
        resume = Resume.get(resume_id)
        job_post = JobPost.get(job_post_id)
        cl_service = CoverLetterService(ai_client, job_post, resume)
        cover_letter = cl_service.generate_cover_letter()
        cover_letter.save()

        ser = self.get_serializer()
        payload = {"data": ser.to_resource(cover_letter)}
        include_rels = self._parse_include(request)
        if include_rels:
            payload["included"] = self._build_included([cover_letter], include_rels)
        return Response(payload, status=status.HTTP_201_CREATED)


class ApplicationViewSet(BaseSAViewSet):
    model = Application
    serializer_class = ApplicationSerializer


class ExperienceViewSet(BaseSAViewSet):
    model = Experience
    serializer_class = ExperienceSerializer


class EducationViewSet(BaseSAViewSet):
    model = Education
    serializer_class = EducationSerializer


class CertificationViewSet(BaseSAViewSet):
    model = Certification
    serializer_class = CertificationSerializer


class DescriptionViewSet(BaseSAViewSet):
    model = Description
    serializer_class = DescriptionSerializer

    @action(detail=True, methods=["get"])
    def experiences(self, request, pk=None):
        obj = self.model.get(int(pk))
        if not obj:
            return Response({"errors": [{"detail": "Not found"}]}, status=404)
        data = [ExperienceSerializer().to_resource(e) for e in (obj.experiences or [])]
        return Response({"data": data})
