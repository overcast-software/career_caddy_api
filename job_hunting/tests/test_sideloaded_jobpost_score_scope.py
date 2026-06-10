"""
Privacy regression — cross-user `top_score` (and friends) leak via sideloaded
JobPost resources and unscoped sub-collection endpoints.

Background:
JobPost rows are shared across users (multi-tenant model). Per-user signals —
the score we gave the post, our applications, our scrapes — live on rows
that FK the user. ``JobPost.top_score`` is a model ``@property`` with a
documented unscoped fallback for shell/fixture contexts. When the view
forgets to attach ``_top_score`` filtered by ``request.user``, that fallback
runs in an API response and returns *another user's* top score for the
shared post. JobPostViewSet.list / retrieve do attach it; every other
sideload path did NOT, so the response shape silently leaked.

The fix lives in three places:

1. ``JobPostSerializer.to_resource`` force-nulls ``top_score`` and the
   ``top-score`` relationship when ``_top_score`` is absent — closes the
   leak at the source.

2. ``BaseViewSet._build_included`` hydrates ``_top_score`` user-scoped
   before calling ``to_resource`` on a sideloaded JobPost — so the
   common case (sideloading via ``?include=``) keeps emitting a real
   value, now correctly scoped.

3. ``companies.job_posts`` (``GET /companies/<id>/job-posts/``) attaches
   ``_top_score`` the same way ``jobs.list`` does.

Adjacent privacy leaks fixed in the same PR:

- ``GET /companies/<id>/scrapes/`` was unscoped; now filtered by
  ``created_by_id`` (staff bypasses).
- ``GET /resumes/<id>/scores/`` returned ``obj.scores.all()`` with no
  user filter AND no resume-ownership check; now both.
- ``GET /<resource>/<id>/relationships/<rel>`` (generic
  ``BaseViewSet.relationships``) was unscoped; now filters by the
  target serializer's ``user_fk`` when the target is per-user (Score,
  CoverLetter, Summary, JobApplication, etc.).
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from job_hunting.models import (
    Company,
    JobApplication,
    JobPost,
    Resume,
    Score,
    Scrape,
)

User = get_user_model()


class _TwoUserSharedJobPostBase(TestCase):
    """Two users (A and B), one shared Company + JobPost, each with their
    own Score on it. A's score is higher — so cross-user leaks show up as
    B seeing 85 (A's number) instead of 40 (B's own).
    """

    def setUp(self):
        self.user_a = User.objects.create_user(username="alice", password="pw")
        self.user_b = User.objects.create_user(username="bob", password="pw")
        self.company = Company.objects.create(name="Acme")
        self.jp = JobPost.objects.create(
            title="Engineer",
            company=self.company,
            created_by=self.user_a,
            description="a " * 80,
        )
        # A's score is the HIGHER of the two — the leak signature.
        self.score_a = Score.objects.create(
            job_post=self.jp, user=self.user_a, score=85,
        )
        self.score_b = Score.objects.create(
            job_post=self.jp, user=self.user_b, score=40,
        )
        # B must have JP visibility (own score grants it via the
        # five-clause filter); A is the creator so already visible.
        self.client_b = APIClient()
        self.client_b.force_authenticate(user=self.user_b)
        self.client_a = APIClient()
        self.client_a.force_authenticate(user=self.user_a)

    def _sideloaded_jp(self, resp, jp_id=None):
        """Pluck the JobPost sideload (or embedded data entry) for `jp_id`
        out of the JSON response. Returns the resource dict or None."""
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        target = str(jp_id if jp_id is not None else self.jp.id)
        for chunk in (body.get("included") or []) + (body.get("data") if isinstance(body.get("data"), list) else []):
            if chunk.get("type") == "job-post" and str(chunk.get("id")) == target:
                return chunk
        # `data` may be a single resource too
        d = body.get("data")
        if isinstance(d, dict) and d.get("type") == "job-post" and str(d.get("id")) == target:
            return d
        return None


class TestSideloadedJobPostTopScoreIsUserScoped(_TwoUserSharedJobPostBase):
    """B's view of the shared JobPost must report B's top score (40),
    NEVER A's (85), no matter which surface produced the JP resource."""

    def test_job_applications_include_jobpost_emits_callers_top_score(self):
        # B has their own JobApplication; sideloading the JP must report B's score.
        JobApplication.objects.create(
            user=self.user_b, job_post=self.jp, company=self.company,
            status="applied",
        )
        resp = self.client_b.get("/api/v1/job-applications/?include=job-post.company")
        jp = self._sideloaded_jp(resp)
        self.assertIsNotNone(jp, "Expected JP sideload in included[]")
        self.assertEqual(jp["attributes"]["top_score"], 40)
        top_rel = jp["relationships"].get("top-score") or {}
        rel_data = top_rel.get("data")
        self.assertIsNotNone(rel_data, "top-score relationship should not be null when caller has a score")
        self.assertEqual(int(rel_data["id"]), self.score_b.id)

    def test_companies_job_posts_subcollection_uses_callers_top_score(self):
        resp = self.client_b.get(f"/api/v1/companies/{self.company.id}/job-posts/")
        jp = self._sideloaded_jp(resp)
        self.assertIsNotNone(jp)
        self.assertEqual(jp["attributes"]["top_score"], 40)
        rel_data = (jp["relationships"].get("top-score") or {}).get("data")
        self.assertIsNotNone(rel_data)
        self.assertEqual(int(rel_data["id"]), self.score_b.id)

    def test_primary_retrieve_still_emits_callers_top_score_regression(self):
        # Pre-existing list/retrieve hydration must keep working: B reads
        # the JP directly and sees their own score, not A's.
        resp = self.client_b.get(f"/api/v1/job-posts/{self.jp.id}/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        jp = resp.json()["data"]
        self.assertEqual(jp["attributes"]["top_score"], 40)

    def test_user_a_still_sees_user_a_top_score(self):
        # Symmetric: A reading the same JP must see 85 (their own).
        resp = self.client_a.get(f"/api/v1/job-posts/{self.jp.id}/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()["data"]["attributes"]["top_score"], 85)


class TestJobPostRelationshipsScoresEndpointIsUserScoped(_TwoUserSharedJobPostBase):
    """``GET /job-posts/<id>/relationships/scores`` (the generic
    BaseViewSet.relationships action) must only surface the requesting
    user's score IDs — Score declares user_fk so the linkage filter fires."""

    def test_relationships_scores_only_surfaces_callers_score_ids(self):
        resp = self.client_b.get(f"/api/v1/job-posts/{self.jp.id}/relationships/scores/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = {int(r["id"]) for r in resp.json()["data"]}
        self.assertIn(self.score_b.id, ids)
        self.assertNotIn(
            self.score_a.id, ids,
            "Generic /relationships/scores must not leak other users' Score IDs",
        )

    def test_staff_relationships_scores_sees_every_id(self):
        staff = User.objects.create_user(username="admin", password="pw", is_staff=True)
        client = APIClient()
        client.force_authenticate(user=staff)
        resp = client.get(f"/api/v1/job-posts/{self.jp.id}/relationships/scores/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = {int(r["id"]) for r in resp.json()["data"]}
        self.assertIn(self.score_a.id, ids)
        self.assertIn(self.score_b.id, ids)


class TestCompanyScrapesEndpointIsUserScoped(_TwoUserSharedJobPostBase):
    """``GET /companies/<id>/scrapes/`` must not leak other users' scrapes."""

    def setUp(self):
        super().setUp()
        self.scrape_a = Scrape.objects.create(
            url="https://acme.test/job/a", company=self.company, created_by=self.user_a,
        )
        self.scrape_b = Scrape.objects.create(
            url="https://acme.test/job/b", company=self.company, created_by=self.user_b,
        )

    def test_bob_only_sees_own_scrapes_on_company(self):
        resp = self.client_b.get(f"/api/v1/companies/{self.company.id}/scrapes/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = {int(r["id"]) for r in resp.json()["data"]}
        self.assertIn(self.scrape_b.id, ids)
        self.assertNotIn(self.scrape_a.id, ids)

    def test_staff_sees_every_scrape_on_company(self):
        staff = User.objects.create_user(username="admin2", password="pw", is_staff=True)
        client = APIClient()
        client.force_authenticate(user=staff)
        resp = client.get(f"/api/v1/companies/{self.company.id}/scrapes/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = {int(r["id"]) for r in resp.json()["data"]}
        self.assertIn(self.scrape_a.id, ids)
        self.assertIn(self.scrape_b.id, ids)


class TestResumeScoresEndpointIsUserScoped(_TwoUserSharedJobPostBase):
    """``GET /resumes/<id>/scores/`` must (a) 404 when the resume isn't
    the caller's, and (b) only surface the caller's scores on their own
    resume — both defenses, not just one."""

    def setUp(self):
        super().setUp()
        self.resume_a = Resume.objects.create(name="Alice Resume", user=self.user_a)
        self.resume_b = Resume.objects.create(name="Bob Resume", user=self.user_b)
        # A score on B's resume that DOES belong to B — must surface.
        self.bobs_score_on_bobs_resume = Score.objects.create(
            job_post=self.jp, resume=self.resume_b, user=self.user_b, score=42,
        )
        # A score on A's resume that belongs to A — must NOT surface for B.
        self.alices_score_on_alices_resume = Score.objects.create(
            job_post=self.jp, resume=self.resume_a, user=self.user_a, score=99,
        )

    def test_bob_cannot_access_alices_resume_scores(self):
        resp = self.client_b.get(f"/api/v1/resumes/{self.resume_a.id}/scores/")
        self.assertEqual(
            resp.status_code, status.HTTP_404_NOT_FOUND,
            "Accessing another user's resume's scores must 404 — not 200 with []",
        )

    def test_bob_sees_own_scores_on_own_resume(self):
        resp = self.client_b.get(f"/api/v1/resumes/{self.resume_b.id}/scores/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = {int(r["id"]) for r in resp.json()["data"]}
        self.assertIn(self.bobs_score_on_bobs_resume.id, ids)
        # No leakage of A's score, even though it's on a different resume.
        self.assertNotIn(self.alices_score_on_alices_resume.id, ids)


class TestAnonymousRequestRejected(_TwoUserSharedJobPostBase):
    """All sideload paths require authentication; anonymous calls must 401."""

    def test_anonymous_companies_jobposts_rejected(self):
        client = APIClient()
        resp = client.get(f"/api/v1/companies/{self.company.id}/job-posts/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_anonymous_jp_retrieve_rejected(self):
        client = APIClient()
        resp = client.get(f"/api/v1/job-posts/{self.jp.id}/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_anonymous_resume_scores_rejected(self):
        client = APIClient()
        resume = Resume.objects.create(name="x", user=self.user_a)
        resp = client.get(f"/api/v1/resumes/{resume.id}/scores/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class TestUnscoredCallerEmitsNullTopScore(_TwoUserSharedJobPostBase):
    """Edge case: user with JP visibility but no Score row of their own
    must see ``top_score: null`` and ``top-score: {data: null}`` — NOT
    the unscoped model-property fallback that would have returned 85.
    """

    def setUp(self):
        super().setUp()
        # Delete B's own score; B keeps visibility via the JobApplication.
        self.score_b.delete()
        JobApplication.objects.create(
            user=self.user_b, job_post=self.jp, company=self.company, status="applied",
        )

    def test_company_jobposts_emits_null_when_caller_has_no_score(self):
        resp = self.client_b.get(f"/api/v1/companies/{self.company.id}/job-posts/")
        jp = self._sideloaded_jp(resp)
        self.assertIsNotNone(jp)
        self.assertIsNone(
            jp["attributes"]["top_score"],
            "No score for caller → top_score must be null, not the unscoped fallback",
        )
        rel_data = (jp["relationships"].get("top-score") or {}).get("data")
        self.assertIsNone(rel_data)

    def test_job_applications_sideload_emits_null_when_caller_has_no_score(self):
        resp = self.client_b.get("/api/v1/job-applications/?include=job-post")
        jp = self._sideloaded_jp(resp)
        self.assertIsNotNone(jp)
        self.assertIsNone(jp["attributes"]["top_score"])
        rel_data = (jp["relationships"].get("top-score") or {}).get("data")
        self.assertIsNone(rel_data)
