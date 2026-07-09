"""Tests for CC-135 — the staff-gated agentic JobPost lookup (MatchRequest).

Two surfaces:
  1. POST/GET /api/v1/match-requests/ — staff-gated (IsAdminUser: 401 anon,
     403 authed non-staff, 202 staff). POST validates url, truncates
     text_excerpt, creates the row, and enqueues the matcher task.
  2. job_hunting.lib.tasks.match_request_job — the qcluster worker leg: candidate
     pre-fetch, one LLM call, choose-from-list guard, zero-candidate + failure
     paths.

Enqueue idiom mirrors CC-122 (test_scrape_from_text): patch
``match_requests.async_task`` and assert the dotted target + args, since
``Q_CLUSTER['sync']`` is NOT on globally under TESTING. The LLM seam is the
same as the DescriptionArbiter tests: patch ``JobMatcher`` /
``JobMatcher._call_llm`` and return a ``MatchDecision``.
"""
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import Company, JobPost, MatchRequest
from job_hunting.models.match_request import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    TEXT_EXCERPT_MAX_LEN,
)
from job_hunting.lib.parsers.job_matcher import MatchDecision

User = get_user_model()

MATCH_TASK = "job_hunting.lib.tasks.match_request_job"


class TestMatchRequestGate(TestCase):
    """IsAdminUser gate: 401 anon, 403 authed non-staff, 202 staff."""

    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user(
            username="staff", password="pw", is_staff=True
        )
        self.plain = User.objects.create_user(username="plain", password="pw")

    def _post(self):
        return self.client.post(
            "/api/v1/match-requests/",
            data={"url": "https://ats.example.com/apply/123"},
            format="json",
        )

    def test_anonymous_is_401(self):
        resp = self._post()
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authed_non_staff_is_403(self):
        self.client.force_authenticate(user=self.plain)
        resp = self._post()
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    @patch("job_hunting.api.views.match_requests.async_task")
    def test_staff_is_202(self, mock_async):
        self.client.force_authenticate(user=self.staff)
        resp = self._post()
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        mock_async.assert_called_once()


class TestMatchRequestCreate(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user(
            username="staff", password="pw", is_staff=True
        )
        self.client.force_authenticate(user=self.staff)

    @patch("job_hunting.api.views.match_requests.async_task")
    def test_happy_path_enqueues_and_returns_pending(self, mock_async):
        resp = self.client.post(
            "/api/v1/match-requests/",
            data={
                "url": "https://ats.example.com/apply/123",
                "referrer": "https://boards.example.org/jobs/42",
                "page_title": "Senior Engineer - Acme",
                "text_excerpt": "We are hiring a Senior Engineer at Acme...",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        body = resp.json()
        self.assertEqual(body["data"]["type"], "match-request")
        self.assertEqual(body["data"]["attributes"]["status"], STATUS_PENDING)

        mr = MatchRequest.objects.get(pk=body["data"]["id"])
        self.assertEqual(mr.created_by, self.staff)
        self.assertEqual(mr.status, STATUS_PENDING)
        self.assertEqual(mr.referrer, "https://boards.example.org/jobs/42")
        self.assertEqual(mr.page_title, "Senior Engineer - Acme")

        mock_async.assert_called_once()
        args, kwargs = mock_async.call_args
        self.assertEqual(args[0], MATCH_TASK)
        self.assertEqual(args[1], mr.id)

    @patch("job_hunting.api.views.match_requests.async_task")
    def test_jsonapi_envelope_accepted(self, mock_async):
        resp = self.client.post(
            "/api/v1/match-requests/",
            data={
                "data": {
                    "type": "match-request",
                    "attributes": {"url": "https://ats.example.com/apply/9"},
                }
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        mock_async.assert_called_once()

    @patch("job_hunting.api.views.match_requests.async_task")
    def test_missing_url_is_400_no_enqueue(self, mock_async):
        resp = self.client.post(
            "/api/v1/match-requests/", data={"referrer": "x"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        mock_async.assert_not_called()

    @patch("job_hunting.api.views.match_requests.async_task")
    def test_invalid_url_is_400_no_enqueue(self, mock_async):
        resp = self.client.post(
            "/api/v1/match-requests/",
            data={"url": "javascript:alert(1)"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        mock_async.assert_not_called()

    @patch("job_hunting.api.views.match_requests.async_task")
    def test_text_excerpt_truncated_at_write(self, mock_async):
        long_text = "x" * (TEXT_EXCERPT_MAX_LEN + 500)
        resp = self.client.post(
            "/api/v1/match-requests/",
            data={"url": "https://ats.example.com/apply/1", "text_excerpt": long_text},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        mr = MatchRequest.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(len(mr.text_excerpt), TEXT_EXCERPT_MAX_LEN)

    @patch("job_hunting.api.views.match_requests.async_task")
    def test_bad_referrer_dropped_not_rejected(self, mock_async):
        resp = self.client.post(
            "/api/v1/match-requests/",
            data={
                "url": "https://ats.example.com/apply/1",
                "referrer": "not a url",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        mr = MatchRequest.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(mr.referrer, "")


class TestMatchRequestRetrievePermission(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user(
            username="staff", password="pw", is_staff=True
        )
        self.other_staff = User.objects.create_user(
            username="staff2", password="pw", is_staff=True
        )
        self.mr = MatchRequest.objects.create(
            created_by=self.staff, url="https://ats.example.com/apply/1"
        )

    def test_creator_can_read(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.get(f"/api/v1/match-requests/{self.mr.id}/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()["data"]["id"], self.mr.id)

    def test_other_staff_can_read(self):
        # Non-owning staff may read (support/debug); the gate only blocks
        # non-staff, and retrieve narrows to creator-or-staff.
        self.client.force_authenticate(user=self.other_staff)
        resp = self.client.get(f"/api/v1/match-requests/{self.mr.id}/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_nonexistent_is_404(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.get("/api/v1/match-requests/ZZZbogusZZ/")
        self.assertEqual(resp.status_code, 404)


class TestMatchRequestTask(TestCase):
    """The qcluster worker leg: candidate pre-fetch + one LLM call."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="staff", password="pw", is_staff=True
        )
        self.company = Company.objects.create(name="Acme")

    def _mr(self, **kw):
        defaults = dict(
            created_by=self.user,
            url="https://ats.example.com/apply/123",
            referrer="https://boards.example.org/jobs/42",
            page_title="Senior Engineer",
        )
        defaults.update(kw)
        return MatchRequest.objects.create(**defaults)

    def _post(self, **kw):
        defaults = dict(
            title="Senior Engineer",
            company=self.company,
            created_by=self.user,
        )
        defaults.update(kw)
        return JobPost.objects.create(**defaults)

    def _run(self, mr):
        from job_hunting.lib.tasks import match_request_job

        return match_request_job(mr.id)

    def _patch_matcher(self, decision):
        """Patch JobMatcher so .match() returns `decision` without an LLM call."""
        matcher = MagicMock()
        matcher.match.return_value = decision
        return patch(
            "job_hunting.lib.parsers.job_matcher.JobMatcher", return_value=matcher
        ), matcher

    def test_zero_candidates_done_null(self):
        # No JobPost matches host or title -> done, null, "no candidates",
        # and NO LLM call.
        mr = self._mr(
            referrer="https://nowhere.invalid/x",
            url="https://nowhere-else.invalid/y",
            page_title="Zzxq Nonexistent Role",
        )
        with patch("job_hunting.lib.parsers.job_matcher.JobMatcher") as MockMatcher:
            self._run(mr)
            MockMatcher.assert_not_called()
        mr.refresh_from_db()
        self.assertEqual(mr.status, STATUS_DONE)
        self.assertIsNone(mr.result_job_post_id)
        self.assertEqual(mr.rationale, "no candidates")

    def test_happy_path_picks_candidate(self):
        # A post on the referrer host is a candidate; the mocked matcher picks it.
        post = self._post(link="https://boards.example.org/jobs/42")
        mr = self._mr()
        decision = MatchDecision(
            job_post_id=post.id, confidence=0.9, rationale="same role at Acme"
        )
        ctx, matcher = self._patch_matcher(decision)
        with ctx:
            self._run(mr)
        matcher.match.assert_called_once()
        mr.refresh_from_db()
        self.assertEqual(mr.status, STATUS_DONE)
        self.assertEqual(mr.result_job_post_id, post.id)
        self.assertEqual(mr.confidence, 0.9)

    def test_matcher_null_pick_recorded(self):
        self._post(link="https://boards.example.org/jobs/42")
        mr = self._mr()
        decision = MatchDecision(
            job_post_id=None, confidence=0.1, rationale="none match"
        )
        ctx, _ = self._patch_matcher(decision)
        with ctx:
            self._run(mr)
        mr.refresh_from_db()
        self.assertEqual(mr.status, STATUS_DONE)
        self.assertIsNone(mr.result_job_post_id)
        self.assertEqual(mr.rationale, "none match")

    def test_invented_id_rejected_as_null(self):
        # The matcher returns an id NOT in the candidate list -> treated as
        # null, and the off-list id is noted in the rationale.
        self._post(link="https://boards.example.org/jobs/42")
        mr = self._mr()
        decision = MatchDecision(
            job_post_id="NOTACANDID", confidence=0.8, rationale="picked one"
        )
        ctx, _ = self._patch_matcher(decision)
        with ctx:
            self._run(mr)
        mr.refresh_from_db()
        self.assertEqual(mr.status, STATUS_DONE)
        self.assertIsNone(mr.result_job_post_id)
        self.assertIn("NOTACANDID", mr.rationale)

    def test_llm_exception_marks_failed(self):
        self._post(link="https://boards.example.org/jobs/42")
        mr = self._mr()
        matcher = MagicMock()
        matcher.match.side_effect = RuntimeError("secret-token-abc timeout")
        with patch(
            "job_hunting.lib.parsers.job_matcher.JobMatcher", return_value=matcher
        ):
            self._run(mr)
        mr.refresh_from_db()
        self.assertEqual(mr.status, STATUS_FAILED)
        self.assertIsNone(mr.result_job_post_id)
        # Safe summary only — no secret / traceback leaked into the rationale.
        self.assertNotIn("secret-token-abc", mr.rationale)
        self.assertIn("RuntimeError", mr.rationale)

    def test_candidate_visibility_scoped_to_user(self):
        # A post another user owns, with no per-user signal for our user, is
        # NOT a candidate — so a non-staff requester gets zero candidates.
        other = User.objects.create_user(username="other", password="pw")
        self._post(
            link="https://boards.example.org/jobs/42",
            created_by=other,
        )
        non_staff = User.objects.create_user(username="req", password="pw")
        mr = self._mr(created_by=non_staff)
        with patch("job_hunting.lib.parsers.job_matcher.JobMatcher") as MockMatcher:
            self._run(mr)
            MockMatcher.assert_not_called()
        mr.refresh_from_db()
        self.assertEqual(mr.status, STATUS_DONE)
        self.assertIsNone(mr.result_job_post_id)
        self.assertEqual(mr.rationale, "no candidates")

    def test_terminal_row_is_noop(self):
        # A requeue of an already-done row does not re-run the matcher.
        post = self._post(link="https://boards.example.org/jobs/42")
        mr = self._mr(status=STATUS_DONE, result_job_post=post)
        with patch("job_hunting.lib.parsers.job_matcher.JobMatcher") as MockMatcher:
            self._run(mr)
            MockMatcher.assert_not_called()
        mr.refresh_from_db()
        self.assertEqual(mr.result_job_post_id, post.id)
