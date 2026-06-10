"""Tests for POST /api/v1/scrape-profiles/:id/sharpen/.

The endpoint enqueues a sharpen pass against the ScrapeProfile, picking
the most-recent successful Scrape for the profile's hostname as the
source page. Staff-only; the rest of the ScrapeProfileViewSet keeps
IsAdminUser.

Coverage:
- unauthenticated → 401
- authenticated non-staff → 403
- staff + no successful scrape for hostname → 422
- staff + valid source scrape → 202, async_task invoked, profile returned

The django-q enqueue is mocked at the view import site so tests don't
require a live qcluster process.

This file also covers GET /api/v1/scrape-profiles/:id/sharpen-status/
— the polling target frontend uses to check on an enqueued sharpen
task. See ``ScrapeProfileSharpenStatusTests`` below.
"""
from unittest.mock import patch

from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import Scrape, ScrapeProfile

User = get_user_model()


class ScrapeProfileSharpenTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.profile = ScrapeProfile.objects.create(
            hostname="example.com",
            enabled=True,
        )

    def _url(self, profile_id=None):
        pid = profile_id if profile_id is not None else self.profile.id
        return f"/api/v1/scrape-profiles/{pid}/sharpen/"

    def test_unauthenticated_returns_401(self):
        client = APIClient()
        resp = client.post(self._url(), {}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_non_staff_returns_403(self):
        user = User.objects.create_user(
            username="nonstaff", password="pw", is_staff=False
        )
        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.post(self._url(), {}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_no_successful_scrape_returns_422(self):
        """No completed scrape for the hostname → 422 with the
        capture-one-first message. The enhancer can't sharpen against
        thin air."""
        user = User.objects.create_user(
            username="staff", password="pw", is_staff=True
        )
        client = APIClient()
        client.force_authenticate(user=user)

        # Throw in an unrelated completed scrape on a different host to
        # confirm the hostname filter actually filters.
        Scrape.objects.create(
            url="https://other.com/jobs/9",
            status="completed",
        )

        resp = client.post(self._url(), {}, format="json")
        self.assertEqual(
            resp.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY
        )
        body = resp.json()
        self.assertIn(
            "No successful scrape", body["errors"][0]["detail"]
        )

    def test_staff_with_completed_scrape_enqueues_and_returns_202(self):
        """Happy path: a completed Scrape exists for the host, the
        endpoint enqueues the task, returns 202 with the profile JSON
        and a meta.job_id."""
        user = User.objects.create_user(
            username="staff", password="pw", is_staff=True
        )
        client = APIClient()
        client.force_authenticate(user=user)

        source = Scrape.objects.create(
            url="https://example.com/jobs/1",
            status="completed",
        )

        with patch(
            "job_hunting.api.views.scrapes.async_task",
            return_value="task-deadbeef",
        ) as mock_async:
            resp = client.post(self._url(), {}, format="json")

        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        body = resp.json()
        self.assertEqual(body["data"]["id"], str(self.profile.id))
        self.assertEqual(body["meta"]["job_id"], "task-deadbeef")
        self.assertEqual(body["meta"]["source_scrape_id"], source.id)

        mock_async.assert_called_once()
        args, kwargs = mock_async.call_args
        self.assertEqual(args[0], "job_hunting.lib.tasks.sharpen_scrape_profile")
        self.assertEqual(args[1], self.profile.id)
        self.assertEqual(kwargs["source_scrape_id"], source.id)
        self.assertEqual(kwargs["requested_by_id"], user.id)

    def test_staff_picks_most_recent_completed_scrape(self):
        """When multiple completed scrapes exist for the host, the
        endpoint picks the newest one as the source."""
        user = User.objects.create_user(
            username="staff", password="pw", is_staff=True
        )
        client = APIClient()
        client.force_authenticate(user=user)

        older = Scrape.objects.create(
            url="https://example.com/jobs/1",
            status="completed",
        )
        newer = Scrape.objects.create(
            url="https://example.com/jobs/2",
            status="completed",
        )
        # Sanity check ordering — Scrape uses scraped_at as completion
        # timestamp; set explicitly under TestCase so the newer row is
        # unambiguously later than the older one.
        from django.utils import timezone as _tz
        older.scraped_at = _tz.now()
        older.save(update_fields=["scraped_at"])
        newer.scraped_at = _tz.now()
        newer.save(update_fields=["scraped_at"])
        self.assertGreater(newer.scraped_at, older.scraped_at)

        with patch(
            "job_hunting.api.views.scrapes.async_task",
            return_value="job-2",
        ) as mock_async:
            resp = client.post(self._url(), {}, format="json")

        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(
            resp.json()["meta"]["source_scrape_id"], newer.id
        )
        kwargs = mock_async.call_args.kwargs
        self.assertEqual(kwargs["source_scrape_id"], newer.id)

    def test_subdomain_url_matches_parent_hostname(self):
        """A profile for example.com finds scrapes against
        jobs.example.com (single profile covers the host family).
        Mirrors the extension-selectors lookup direction."""
        user = User.objects.create_user(
            username="staff", password="pw", is_staff=True
        )
        client = APIClient()
        client.force_authenticate(user=user)

        sub = Scrape.objects.create(
            url="https://jobs.example.com/posts/abc",
            status="completed",
        )

        with patch(
            "job_hunting.api.views.scrapes.async_task",
            return_value="job-sub",
        ):
            resp = client.post(self._url(), {}, format="json")

        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(resp.json()["meta"]["source_scrape_id"], sub.id)

    def test_unknown_profile_returns_404(self):
        user = User.objects.create_user(
            username="staff", password="pw", is_staff=True
        )
        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.post(self._url(profile_id=999999), {}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


class SharpenTaskTests(TestCase):
    """Direct tests of the task body — bypass the endpoint, verify the
    request gets recorded onto the profile."""

    @classmethod
    def setUpTestData(cls):
        cls.profile = ScrapeProfile.objects.create(
            hostname="example.com",
            extraction_hints="prior hint",
        )
        cls.scrape = Scrape.objects.create(
            url="https://example.com/jobs/1",
            status="completed",
        )

    def test_records_request_into_extraction_hints(self):
        from job_hunting.lib.tasks import sharpen_scrape_profile

        result = sharpen_scrape_profile(
            self.profile.id,
            source_scrape_id=self.scrape.id,
            requested_by_id=42,
        )
        self.assertEqual(result["status"], "requested")
        self.assertEqual(result["hostname"], "example.com")

        self.profile.refresh_from_db()
        self.assertIn("prior hint", self.profile.extraction_hints)
        self.assertIn("sharpen-request", self.profile.extraction_hints)
        self.assertIn("requested_by=42", self.profile.extraction_hints)
        self.assertIn(
            f"source_scrape={self.scrape.id}", self.profile.extraction_hints
        )

    def test_missing_profile_returns_status_missing(self):
        from job_hunting.lib.tasks import sharpen_scrape_profile

        result = sharpen_scrape_profile(
            999999,
            source_scrape_id=self.scrape.id,
        )
        self.assertEqual(result["status"], "missing")

    def test_missing_source_scrape_returns_status_source_missing(self):
        from job_hunting.lib.tasks import sharpen_scrape_profile

        result = sharpen_scrape_profile(
            self.profile.id,
            source_scrape_id=999999,
        )
        self.assertEqual(result["status"], "source_missing")


class ScrapeProfileSharpenStatusTests(TestCase):
    """Tests for GET /api/v1/scrape-profiles/:id/sharpen-status/?job_id=...

    The endpoint inspects django-q's Task table (Success / Failure
    proxy managers) and the OrmQ queued-rows table to report one of
    completed / failed / pending / unknown. Always 200 for valid
    requests — the status string carries the meaning.
    """

    @classmethod
    def setUpTestData(cls):
        cls.profile = ScrapeProfile.objects.create(
            hostname="example.com",
            enabled=True,
        )

    def _url(self, profile_id=None, job_id=None):
        pid = profile_id if profile_id is not None else self.profile.id
        base = f"/api/v1/scrape-profiles/{pid}/sharpen-status/"
        if job_id is not None:
            return f"{base}?job_id={job_id}"
        return base

    def _staff_client(self):
        user = User.objects.create_user(
            username="staff", password="pw", is_staff=True
        )
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _make_task_row(self, *, task_id, success, result, name="t"):
        """Create a row in the django-q Task table. Success / Failure
        are proxy managers on Task discriminated by the ``success``
        boolean, so we insert through Task and let the view's proxy
        queries pick it up."""
        from django_q.models import Task

        now = timezone.now()
        return Task.objects.create(
            id=task_id,
            name=name,
            func="job_hunting.lib.tasks.sharpen_scrape_profile",
            started=now,
            stopped=now,
            success=success,
            result=result,
            attempt_count=1,
        )

    def test_unauthenticated_returns_401(self):
        client = APIClient()
        resp = client.get(self._url(job_id="anything"))
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_non_staff_returns_403(self):
        user = User.objects.create_user(
            username="nonstaff", password="pw", is_staff=False
        )
        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.get(self._url(job_id="anything"))
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_missing_job_id_returns_400(self):
        client = self._staff_client()
        resp = client.get(self._url())  # no ?job_id=
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        body = resp.json()
        self.assertIn("job_id", body["errors"][0]["detail"])

    def test_empty_job_id_returns_400(self):
        client = self._staff_client()
        resp = client.get(self._url(job_id=""))
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_profile_returns_404(self):
        client = self._staff_client()
        resp = client.get(self._url(profile_id=999999, job_id="anything"))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_success_returns_completed(self):
        """A Task row with success=True → status=completed, result
        comes through, timestamps present."""
        client = self._staff_client()
        job_id = "a" * 32
        self._make_task_row(
            task_id=job_id,
            success=True,
            result={"status": "requested", "hostname": "example.com"},
        )

        resp = client.get(self._url(job_id=job_id))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        attrs = body["data"]["attributes"]
        self.assertEqual(body["data"]["type"], "scrape-profile-sharpen-status")
        self.assertEqual(body["data"]["id"], job_id)
        self.assertEqual(attrs["status"], "completed")
        self.assertEqual(
            attrs["result"],
            {"status": "requested", "hostname": "example.com"},
        )
        self.assertIsNone(attrs["error"])
        self.assertIsNotNone(attrs["started_at"])
        self.assertIsNotNone(attrs["stopped_at"])

    def test_failure_returns_failed(self):
        """A Task row with success=False → status=failed, error text
        comes through."""
        client = self._staff_client()
        job_id = "b" * 32
        self._make_task_row(
            task_id=job_id,
            success=False,
            result="Traceback (most recent call last): ... RuntimeError: boom",
        )

        resp = client.get(self._url(job_id=job_id))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        attrs = resp.json()["data"]["attributes"]
        self.assertEqual(attrs["status"], "failed")
        self.assertIn("RuntimeError: boom", attrs["error"])
        self.assertIsNone(attrs["result"])
        self.assertIsNotNone(attrs["stopped_at"])

    def test_pending_returns_pending(self):
        """A job id that only appears in OrmQ (queued, not yet
        executed) → status=pending. The view walks OrmQ rows because
        OrmQ.key is the cluster name, not the task id; the id lives
        inside the signed payload. Patch OrmQ to control what it
        sees without depending on django-q's signing internals."""
        client = self._staff_client()
        job_id = "c" * 32

        class _FakeQueued:
            def __init__(self, tid):
                self._tid = tid

            def task_id(self):
                return self._tid

        with patch(
            "django_q.models.OrmQ.objects.all",
            return_value=[_FakeQueued(job_id)],
        ):
            resp = client.get(self._url(job_id=job_id))

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        attrs = resp.json()["data"]["attributes"]
        self.assertEqual(attrs["status"], "pending")
        self.assertIsNone(attrs["result"])
        self.assertIsNone(attrs["error"])
        self.assertIsNone(attrs["started_at"])
        self.assertIsNone(attrs["stopped_at"])

    def test_unknown_returns_unknown(self):
        """A job id not in Success, Failure, or OrmQ → status=unknown
        (job was never enqueued OR records have been pruned)."""
        client = self._staff_client()
        resp = client.get(self._url(job_id="d" * 32))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        attrs = resp.json()["data"]["attributes"]
        self.assertEqual(attrs["status"], "unknown")
        self.assertIsNone(attrs["result"])
        self.assertIsNone(attrs["error"])

    def test_malformed_ormq_row_does_not_500(self):
        """A queued row whose payload can't be decoded must not crash
        the status endpoint — the view treats it as not-this-job and
        moves on."""
        client = self._staff_client()
        job_id = "e" * 32

        class _BrokenQueued:
            def task_id(self):
                raise ValueError("signed-package decode error")

        with patch(
            "django_q.models.OrmQ.objects.all",
            return_value=[_BrokenQueued()],
        ):
            resp = client.get(self._url(job_id=job_id))

        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            resp.json()["data"]["attributes"]["status"], "unknown"
        )
