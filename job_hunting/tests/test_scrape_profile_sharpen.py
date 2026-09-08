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
- CC-238 source selection (``ScrapeProfileSharpenSourceSelectionTests``):
  DOM-less scrapes are never chosen (NULL, ``""`` and whitespace-only
  html alike), NULL ``scraped_at`` sorts last rather than first, and an
  all-DOM-less host gets its own 422 instead of a false 202.

The django-q enqueue is mocked at the view import site so tests don't
require a live qcluster process.

The sharpen-status endpoint that used to be covered here was
removed in CC-208 — it existed only to read django-q2's Task/OrmQ
tables, and meta.job_id had already been permanently null.
— the polling target frontend uses to check on an enqueued sharpen
task. See ``ScrapeProfileSharpenStatusTests`` below.
"""
from datetime import timedelta
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
        endpoint enqueues the task and returns 202 with the profile JSON.
        CC-207b: dispatch is via enqueue('sharpen_scrape_profile', ...),
        which returns no task id — meta.job_id is now null."""
        user = User.objects.create_user(
            username="staff", password="pw", is_staff=True
        )
        client = APIClient()
        client.force_authenticate(user=user)

        source = Scrape.objects.create(
            url="https://example.com/jobs/1",
            status="completed",
            html="<html><body>job</body></html>",
        )

        with patch("job_hunting.api.views.scrapes.enqueue") as mock_async:
            resp = client.post(self._url(), {}, format="json")

        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        body = resp.json()
        self.assertEqual(body["data"]["id"], str(self.profile.id))
        # enqueue() has no task id — meta.job_id is null now.
        self.assertIsNone(body["meta"]["job_id"])
        self.assertEqual(body["meta"]["source_scrape_id"], source.id)

        mock_async.assert_called_once()
        args, kwargs = mock_async.call_args
        self.assertEqual(args[0], "sharpen_scrape_profile")
        self.assertEqual(kwargs["profile_id"], self.profile.id)
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
            html="<html><body>older</body></html>",
        )
        newer = Scrape.objects.create(
            url="https://example.com/jobs/2",
            status="completed",
            html="<html><body>newer</body></html>",
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

        with patch("job_hunting.api.views.scrapes.enqueue") as mock_async:
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
            html="<html><body>sub</body></html>",
        )

        with patch("job_hunting.api.views.scrapes.enqueue"):
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


class ScrapeProfileSharpenSourceSelectionTests(TestCase):
    """CC-238 — the source scrape must actually carry a DOM, and NULL
    ``scraped_at`` must not win the recency sort.

    Production symptom: on hosts whose corpus arrives via the extension /
    from-text / email paths, ``scraped_at`` and ``html`` are both NULL.
    ``ORDER BY scraped_at DESC`` puts NULLs FIRST on PostgreSQL, so
    ``.first()`` handed the enhancer an arbitrary DOM-less row — on
    jobright.ai, the same unrelated ClassDojo posting on two sharpen
    passes a month apart, with ~200 newer scrapes in between.

    These assertions are Postgres-specific by design: NULL ordering is
    exactly the behaviour under test.
    """

    @classmethod
    def setUpTestData(cls):
        cls.profile = ScrapeProfile.objects.create(
            hostname="example.com",
            enabled=True,
        )

    def setUp(self):
        self.user = User.objects.create_user(
            username="staff", password="pw", is_staff=True
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _url(self):
        return f"/api/v1/scrape-profiles/{self.profile.id}/sharpen/"

    def _post(self, body=None):
        with patch("job_hunting.api.views.scrapes.enqueue") as mock_enqueue:
            resp = self.client.post(self._url(), body or {}, format="json")
        return resp, mock_enqueue

    def test_html_less_scrape_loses_even_when_it_is_the_newest(self):
        """Isolates the missing DOM filter: the DOM-less row is
        unambiguously the most recent, so only ``exclude(html…)`` can
        keep it from being chosen."""
        with_html = Scrape.objects.create(
            url="https://example.com/jobs/keeper",
            status="completed",
            html="<html><body>real page</body></html>",
            scraped_at=timezone.now() - timedelta(hours=2),
        )
        Scrape.objects.create(
            url="https://example.com/jobs/newer-but-empty",
            status="completed",
            html=None,
            scraped_at=timezone.now(),
        )

        resp, mock_enqueue = self._post()

        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(
            resp.json()["meta"]["source_scrape_id"], with_html.id
        )
        self.assertEqual(
            mock_enqueue.call_args.kwargs["source_scrape_id"], with_html.id
        )

    def test_empty_string_html_is_treated_as_no_dom(self):
        """``html=""`` is as useless to the enhancer as NULL."""
        with_html = Scrape.objects.create(
            url="https://example.com/jobs/keeper",
            status="completed",
            html="<html><body>real page</body></html>",
            scraped_at=timezone.now() - timedelta(hours=2),
        )
        Scrape.objects.create(
            url="https://example.com/jobs/blank",
            status="completed",
            html="",
            scraped_at=timezone.now(),
        )

        resp, _ = self._post()

        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(
            resp.json()["meta"]["source_scrape_id"], with_html.id
        )

    def test_null_scraped_at_sorts_last_not_first(self):
        """Isolates the NULLS-FIRST bug: both rows carry a DOM, so the
        html filter cannot rescue this one — only ``nulls_last=True``
        can. Under ``ORDER BY scraped_at DESC`` on PostgreSQL the NULL
        row comes back first."""
        timestamped = Scrape.objects.create(
            url="https://example.com/jobs/timestamped",
            status="completed",
            html="<html><body>timestamped</body></html>",
            scraped_at=timezone.now(),
        )
        Scrape.objects.create(
            url="https://example.com/jobs/no-timestamp",
            status="completed",
            html="<html><body>no timestamp</body></html>",
            scraped_at=None,
        )

        resp, mock_enqueue = self._post()

        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(
            resp.json()["meta"]["source_scrape_id"], timestamped.id
        )
        self.assertEqual(
            mock_enqueue.call_args.kwargs["source_scrape_id"], timestamped.id
        )

    def test_ticket_repro_domless_null_row_loses_to_html_bearing_row(self):
        """The literal CC-238 shape, seeded so that *either* mechanism
        failing alone breaks the assertion.

        A single row with html=None AND scraped_at=None is not a real
        repro: the html filter and the NULLS-LAST ordering each demote
        it on their own, so reverting one leaves the other covering the
        gap and the test still passes. Seed one distractor per
        mechanism instead:

        - ``domless_newer`` carries no html but the newest
          ``scraped_at``, so only ``exclude(html…)`` can stop it.
        - ``null_timestamp`` carries html but a NULL ``scraped_at``, so
          only ``nulls_last=True`` can stop it (Postgres sorts NULLs
          FIRST under a bare DESC).

        Both must hold for ``captured`` — the real browser capture — to
        win.
        """
        domless_newer = Scrape.objects.create(
            url="https://example.com/imp_id=x_digest_job_alert_y",
            status="completed",
            html=None,
            scraped_at=timezone.now() + timedelta(hours=1),
        )
        null_timestamp = Scrape.objects.create(
            url="https://example.com/jobs/extension-push",
            status="completed",
            html="<html><body>extension push</body></html>",
            scraped_at=None,
        )
        captured = Scrape.objects.create(
            url="https://example.com/jobs/golden-analytics",
            status="completed",
            html="<html><body>Security Engineer</body></html>",
            scraped_at=timezone.now(),
        )

        resp, mock_enqueue = self._post()

        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(
            resp.json()["meta"]["source_scrape_id"], captured.id
        )
        self.assertEqual(
            mock_enqueue.call_args.kwargs["source_scrape_id"], captured.id
        )
        self.assertNotIn(
            resp.json()["meta"]["source_scrape_id"],
            {domless_newer.id, null_timestamp.id},
        )

    def test_whitespace_only_html_is_treated_as_no_dom(self):
        """``html="\\n  "`` is as useless to the enhancer as "" or NULL.

        ``exclude(html="")`` alone lets it through and re-creates the
        silent no-op 202 the ticket is about.
        """
        with_html = Scrape.objects.create(
            url="https://example.com/jobs/keeper",
            status="completed",
            html="<html><body>real page</body></html>",
            scraped_at=timezone.now() - timedelta(hours=2),
        )
        Scrape.objects.create(
            url="https://example.com/jobs/whitespace",
            status="completed",
            html="\n  \t\n",
            scraped_at=timezone.now(),
        )

        resp, mock_enqueue = self._post()

        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(
            resp.json()["meta"]["source_scrape_id"], with_html.id
        )
        self.assertEqual(
            mock_enqueue.call_args.kwargs["source_scrape_id"], with_html.id
        )

    def test_whitespace_only_host_corpus_returns_422(self):
        """A host whose every completed scrape is whitespace-only gets
        the DOM-less 422, not a 202 with nothing to analyze."""
        Scrape.objects.create(
            url="https://example.com/jobs/whitespace",
            status="completed",
            html="   \n",
            scraped_at=timezone.now(),
        )

        with patch("job_hunting.api.views.scrapes.enqueue") as mock_enqueue:
            resp = self.client.post(self._url(), {}, format="json")

        self.assertEqual(
            resp.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY
        )
        self.assertIn(
            "No captured DOM for this host",
            resp.json()["errors"][0]["detail"],
        )
        mock_enqueue.assert_not_called()

    def test_all_candidates_dom_less_returns_422_not_false_202(self):
        """The operator-facing half of the bug: completed scrapes exist
        for the host but none has a DOM. Before CC-238 this returned a
        202 and a no-op pass; now it says so."""
        Scrape.objects.create(
            url="https://example.com/jobs/1",
            status="completed",
            html=None,
            scraped_at=None,
        )
        Scrape.objects.create(
            url="https://example.com/jobs/2",
            status="completed",
            html="",
            scraped_at=timezone.now(),
        )

        with patch("job_hunting.api.views.scrapes.enqueue") as mock_enqueue:
            resp = self.client.post(self._url(), {}, format="json")

        self.assertEqual(
            resp.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY
        )
        detail = resp.json()["errors"][0]["detail"]
        self.assertIn("No captured DOM for this host", detail)
        mock_enqueue.assert_not_called()

    def test_no_scrape_at_all_keeps_the_original_422_message(self):
        """The two 422s are distinct: nothing scraped is a different
        remedy from scraped-but-DOM-less."""
        with patch("job_hunting.api.views.scrapes.enqueue"):
            resp = self.client.post(self._url(), {}, format="json")

        self.assertEqual(
            resp.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY
        )
        detail = resp.json()["errors"][0]["detail"]
        self.assertIn("No successful scrape", detail)
        self.assertNotIn("No captured DOM", detail)

    def test_unknown_body_keys_are_ignored_not_honoured(self):
        """The endpoint takes no source selector.

        An earlier CC-238 draft accepted ``source_scrape_id`` in the
        body and looked it up with no host or status constraint, which
        would have let a profile be sharpened from a foreign site's DOM.
        That hunk is gone; a body carrying the key must be ignored and
        the host-wide pick must stand.
        """
        foreign = Scrape.objects.create(
            url="https://linkedin.com/jobs/not-this-host",
            status="completed",
            html="<html><body>foreign host</body></html>",
            scraped_at=timezone.now(),
        )
        keeper = Scrape.objects.create(
            url="https://example.com/jobs/1",
            status="completed",
            html="<html><body>x</body></html>",
            scraped_at=timezone.now() - timedelta(hours=1),
        )

        resp, mock_enqueue = self._post({"source_scrape_id": foreign.id})

        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(resp.json()["meta"]["source_scrape_id"], keeper.id)
        self.assertEqual(
            mock_enqueue.call_args.kwargs["source_scrape_id"], keeper.id
        )


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


