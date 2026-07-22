"""Tests for CC-135 (refolded) — the staff-gated agentic JobPost lookup folded
into JobApplication.

Two surfaces:
  1. POST/GET /api/v1/job-applications/ — an ordinary JA create is unaffected;
     a create whose match_context carries inputs is the match TRIGGER, which is
     staff-gated (401 anon, 403 authed non-staff, 202 staff). The trigger
     validates tracking_url, drops a bad referrer, truncates text_excerpt, seeds
     match_context.status=pending, and enqueues the matcher task. GET reads
     match_context back for the extension poll (creator-only, no existence leak).
  2. job_hunting.lib.tasks.job_application_match_job — the qcluster worker leg:
     candidate pre-fetch, one LLM call, choose-from-list guard, zero-candidate +
     failure paths, and job_post backfill on a pick.

Enqueue idiom (CC-205): patch ``jobs.enqueue`` and assert the unified
producer seam — ``enqueue('job_application_match', application_id=<ja>)`` —
instead of the retired ``async_task(dotted_path, ja.id)``. The LLM seam is the
same as the DescriptionArbiter tests: patch ``JobMatcher`` and return a
``MatchDecision``.
"""
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import Company, JobPost, JobApplication
from job_hunting.models.job_application import (
    MATCH_STATUS_DONE,
    MATCH_STATUS_FAILED,
    MATCH_STATUS_PENDING,
    MATCH_TEXT_EXCERPT_MAX_LEN,
)
from job_hunting.lib.parsers.job_matcher import MatchDecision

User = get_user_model()

# CC-205: the JA-match path now dispatches through the unified async producer
# enqueue('job_application_match', application_id=<ja NanoID>) instead of
# async_task(dotted_path, ja.id). Tests assert that seam.
MATCH_KIND = "job_application_match"


class TestMatchTriggerGate(TestCase):
    """The match trigger is staff-gated: 401 anon, 403 authed non-staff, 202 staff.

    A normal JA create (no match_context) is NOT gated — covered separately.
    """

    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user(
            username="staff", password="pw", is_staff=True
        )
        self.plain = User.objects.create_user(username="plain", password="pw")

    def _post(self):
        return self.client.post(
            "/api/v1/job-applications/",
            data={
                "tracking_url": "https://ats.example.com/apply/123",
                "match_context": {"page_title": "Senior Engineer"},
            },
            format="json",
        )

    def test_anonymous_is_401(self):
        resp = self._post()
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authed_non_staff_is_403(self):
        self.client.force_authenticate(user=self.plain)
        resp = self._post()
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    @patch("job_hunting.api.views.jobs.enqueue")
    def test_staff_is_202(self, mock_async):
        self.client.force_authenticate(user=self.staff)
        resp = self._post()
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        mock_async.assert_called_once()


class TestMatchTriggerCreate(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user(
            username="staff", password="pw", is_staff=True
        )
        self.client.force_authenticate(user=self.staff)

    @patch("job_hunting.api.views.jobs.enqueue")
    def test_happy_path_enqueues_and_returns_pending(self, mock_async):
        resp = self.client.post(
            "/api/v1/job-applications/",
            data={
                "tracking_url": "https://ats.example.com/apply/123",
                "match_context": {
                    "referrer": "https://boards.example.org/jobs/42",
                    "page_title": "Senior Engineer - Acme",
                    "text_excerpt": "We are hiring a Senior Engineer at Acme...",
                },
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        body = resp.json()
        self.assertEqual(body["data"]["type"], "job-application")
        self.assertEqual(
            body["data"]["attributes"]["match_context"]["status"], MATCH_STATUS_PENDING
        )

        ja = JobApplication.objects.get(pk=body["data"]["id"])
        self.assertEqual(ja.user_id, self.staff.id)
        self.assertEqual(ja.tracking_url, "https://ats.example.com/apply/123")
        self.assertEqual(ja.match_context["status"], MATCH_STATUS_PENDING)
        self.assertEqual(
            ja.match_context["referrer"], "https://boards.example.org/jobs/42"
        )
        self.assertEqual(ja.match_context["page_title"], "Senior Engineer - Acme")

        mock_async.assert_called_once()
        args, kwargs = mock_async.call_args
        # enqueue('job_application_match', application_id=<ja NanoID string>)
        self.assertEqual(args[0], MATCH_KIND)
        self.assertEqual(kwargs["application_id"], ja.id)
        self.assertIsInstance(kwargs["application_id"], str)

    @patch("job_hunting.api.views.jobs.enqueue")
    def test_jsonapi_envelope_accepted(self, mock_async):
        resp = self.client.post(
            "/api/v1/job-applications/",
            data={
                "data": {
                    "type": "job-application",
                    "attributes": {
                        "tracking_url": "https://ats.example.com/apply/9",
                        "match_context": {"page_title": "Engineer"},
                    },
                }
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        mock_async.assert_called_once()

    @patch("job_hunting.api.views.jobs.enqueue")
    def test_missing_tracking_url_is_400_no_enqueue(self, mock_async):
        resp = self.client.post(
            "/api/v1/job-applications/",
            data={"match_context": {"page_title": "Engineer"}},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        mock_async.assert_not_called()

    @patch("job_hunting.api.views.jobs.enqueue")
    def test_invalid_tracking_url_is_400_no_enqueue(self, mock_async):
        resp = self.client.post(
            "/api/v1/job-applications/",
            data={
                "tracking_url": "javascript:alert(1)",
                "match_context": {"page_title": "Engineer"},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        mock_async.assert_not_called()

    @patch("job_hunting.api.views.jobs.enqueue")
    def test_text_excerpt_truncated_at_write(self, mock_async):
        long_text = "x" * (MATCH_TEXT_EXCERPT_MAX_LEN + 500)
        resp = self.client.post(
            "/api/v1/job-applications/",
            data={
                "tracking_url": "https://ats.example.com/apply/1",
                "match_context": {"page_title": "Engineer", "text_excerpt": long_text},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        ja = JobApplication.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(
            len(ja.match_context["text_excerpt"]), MATCH_TEXT_EXCERPT_MAX_LEN
        )

    @patch("job_hunting.api.views.jobs.enqueue")
    def test_bad_referrer_dropped_not_rejected(self, mock_async):
        resp = self.client.post(
            "/api/v1/job-applications/",
            data={
                "tracking_url": "https://ats.example.com/apply/1",
                "match_context": {"page_title": "Engineer", "referrer": "not a url"},
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        ja = JobApplication.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(ja.match_context["referrer"], "")

    @patch("job_hunting.api.views.jobs.enqueue")
    def test_normal_create_unaffected(self, mock_async):
        # A JA create with NO match_context is the ordinary path: 201, no
        # gating (staff not required), no enqueue, match_context stays null.
        plain = User.objects.create_user(username="normie", password="pw")
        self.client.force_authenticate(user=plain)
        resp = self.client.post(
            "/api/v1/job-applications/",
            data={"status": "Applied", "notes": "cold apply"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        mock_async.assert_not_called()
        ja = JobApplication.objects.get(pk=resp.json()["data"]["id"])
        self.assertIsNone(ja.match_context)
        self.assertEqual(ja.user_id, plain.id)


class TestMatchRetrievePermission(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user(
            username="staff", password="pw", is_staff=True
        )
        self.other = User.objects.create_user(username="other", password="pw")
        self.ja = JobApplication.objects.create(
            user=self.staff,
            tracking_url="https://ats.example.com/apply/1",
            match_context={"status": MATCH_STATUS_PENDING, "page_title": "Engineer"},
        )

    def test_creator_can_read_match_context(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.get(f"/api/v1/job-applications/{self.ja.id}/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(body["data"]["id"], self.ja.id)
        self.assertEqual(
            body["data"]["attributes"]["match_context"]["status"],
            MATCH_STATUS_PENDING,
        )

    def test_other_user_is_404_no_existence_leak(self):
        # A JA that isn't yours is 404, not 403 — the retrieve scoping is
        # creator-only and must not leak the row's existence.
        self.client.force_authenticate(user=self.other)
        resp = self.client.get(f"/api/v1/job-applications/{self.ja.id}/")
        self.assertEqual(resp.status_code, 404)

    def test_nonexistent_is_404(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.get("/api/v1/job-applications/ZZZbogusZZ/")
        self.assertEqual(resp.status_code, 404)


class TestMatchTask(TestCase):
    """The qcluster worker leg: candidate pre-fetch + one LLM call + backfill."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="staff", password="pw", is_staff=True
        )
        self.company = Company.objects.create(name="Acme")

    def _ja(self, **ctx):
        defaults = dict(
            referrer="https://boards.example.org/jobs/42",
            page_title="Senior Engineer",
            text_excerpt="",
            status=MATCH_STATUS_PENDING,
            confidence=None,
            rationale="",
            requested_at="2026-07-09T00:00:00",
            finished_at=None,
        )
        defaults.update(ctx)
        user = defaults.pop("_user", self.user)
        tracking_url = defaults.pop(
            "_tracking_url", "https://ats.example.com/apply/123"
        )
        ja_status = defaults.pop("_status", None)
        return JobApplication.objects.create(
            user=user,
            tracking_url=tracking_url,
            status=ja_status,
            match_context=defaults,
        )

    def _post(self, **kw):
        defaults = dict(
            title="Senior Engineer",
            company=self.company,
            created_by=self.user,
        )
        defaults.update(kw)
        return JobPost.objects.create(**defaults)

    def _run(self, ja):
        from job_hunting.lib.tasks import job_application_match_job

        return job_application_match_job(ja.id)

    def _patch_matcher(self, decision):
        """Patch JobMatcher so .match() returns `decision` without an LLM call."""
        matcher = MagicMock()
        matcher.match.return_value = decision
        return patch(
            "job_hunting.lib.parsers.job_matcher.JobMatcher", return_value=matcher
        ), matcher

    def test_zero_candidates_done_null(self):
        # No JobPost matches host or title -> done, null, "no candidates",
        # and NO LLM call. job_post stays unlinked.
        ja = self._ja(
            referrer="https://nowhere.invalid/x",
            page_title="Zzxq Nonexistent Role",
            _tracking_url="https://nowhere-else.invalid/y",
        )
        with patch("job_hunting.lib.parsers.job_matcher.JobMatcher") as MockMatcher:
            self._run(ja)
            MockMatcher.assert_not_called()
        ja.refresh_from_db()
        self.assertEqual(ja.match_context["status"], MATCH_STATUS_DONE)
        self.assertIsNone(ja.job_post_id)
        self.assertEqual(ja.match_context["rationale"], "no candidates")

    def test_happy_path_picks_candidate_backfills_job_post(self):
        # A post on the referrer host is a candidate; the mocked matcher picks
        # it, and the JA's job_post FK is backfilled directly.
        post = self._post(link="https://boards.example.org/jobs/42")
        ja = self._ja()
        decision = MatchDecision(
            job_post_id=post.id, confidence=0.9, rationale="same role at Acme"
        )
        ctx, matcher = self._patch_matcher(decision)
        with ctx:
            self._run(ja)
        matcher.match.assert_called_once()
        ja.refresh_from_db()
        self.assertEqual(ja.match_context["status"], MATCH_STATUS_DONE)
        self.assertEqual(ja.job_post_id, post.id)
        self.assertEqual(ja.match_context["confidence"], 0.9)

    def test_matcher_null_pick_recorded(self):
        self._post(link="https://boards.example.org/jobs/42")
        ja = self._ja()
        decision = MatchDecision(
            job_post_id=None, confidence=0.1, rationale="none match"
        )
        ctx, _ = self._patch_matcher(decision)
        with ctx:
            self._run(ja)
        ja.refresh_from_db()
        self.assertEqual(ja.match_context["status"], MATCH_STATUS_DONE)
        self.assertIsNone(ja.job_post_id)
        self.assertEqual(ja.match_context["rationale"], "none match")

    def test_invented_id_rejected_as_null(self):
        # The matcher returns an id NOT in the candidate list -> treated as
        # null, and the off-list id is noted in the rationale.
        self._post(link="https://boards.example.org/jobs/42")
        ja = self._ja()
        decision = MatchDecision(
            job_post_id="NOTACANDID", confidence=0.8, rationale="picked one"
        )
        ctx, _ = self._patch_matcher(decision)
        with ctx:
            self._run(ja)
        ja.refresh_from_db()
        self.assertEqual(ja.match_context["status"], MATCH_STATUS_DONE)
        self.assertIsNone(ja.job_post_id)
        self.assertIn("NOTACANDID", ja.match_context["rationale"])

    def test_llm_exception_marks_failed(self):
        self._post(link="https://boards.example.org/jobs/42")
        ja = self._ja()
        matcher = MagicMock()
        matcher.match.side_effect = RuntimeError("secret-token-abc timeout")
        with patch(
            "job_hunting.lib.parsers.job_matcher.JobMatcher", return_value=matcher
        ):
            self._run(ja)
        ja.refresh_from_db()
        self.assertEqual(ja.match_context["status"], MATCH_STATUS_FAILED)
        self.assertIsNone(ja.job_post_id)
        # Safe summary only — no secret / traceback leaked into the rationale.
        self.assertNotIn("secret-token-abc", ja.match_context["rationale"])
        self.assertIn("RuntimeError", ja.match_context["rationale"])

    def test_candidate_visibility_scoped_to_user(self):
        # A post another user owns, with no per-user signal for our applicant,
        # is NOT a candidate — so a non-staff applicant gets zero candidates.
        other = User.objects.create_user(username="poster", password="pw")
        self._post(
            link="https://boards.example.org/jobs/42",
            created_by=other,
        )
        non_staff = User.objects.create_user(username="req", password="pw")
        ja = self._ja(_user=non_staff)
        with patch("job_hunting.lib.parsers.job_matcher.JobMatcher") as MockMatcher:
            self._run(ja)
            MockMatcher.assert_not_called()
        ja.refresh_from_db()
        self.assertEqual(ja.match_context["status"], MATCH_STATUS_DONE)
        self.assertIsNone(ja.job_post_id)
        self.assertEqual(ja.match_context["rationale"], "no candidates")

    def test_terminal_row_is_noop(self):
        # A requeue of an already-done row does not re-run the matcher.
        post = self._post(link="https://boards.example.org/jobs/42")
        ja = self._ja(status=MATCH_STATUS_DONE)
        ja.job_post = post
        ja.save(update_fields=["job_post"])
        with patch("job_hunting.lib.parsers.job_matcher.JobMatcher") as MockMatcher:
            self._run(ja)
            MockMatcher.assert_not_called()
        ja.refresh_from_db()
        self.assertEqual(ja.job_post_id, post.id)
