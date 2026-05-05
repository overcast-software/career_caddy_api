"""Tests for POST /api/v1/scrapes/from-text/ — the paste-to-JobPost endpoint."""

from unittest.mock import patch

from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import Company, JobPost, Scrape

User = get_user_model()


# Existing tests predate the Phase 0 length floor; relax it so they keep
# focusing on what they were written to test. Length bounds get their own
# coverage in TestScrapeFromTextPolicy below.
@patch("job_hunting.api.views.scrapes.FROM_TEXT_MIN_LEN", 1)
class TestScrapeFromText(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="paster", password="pw")
        self.client.force_authenticate(user=self.user)

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_happy_path_returns_202_with_scrape(self, mock_parse):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Senior Engineer at Acme. Remote. $180k-$220k."},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        body = resp.json()
        self.assertIn("data", body)
        self.assertEqual(body["data"]["type"], "scrape")

        scrape = Scrape.objects.get(pk=int(body["data"]["id"]))
        self.assertEqual(scrape.status, "pending")
        self.assertEqual(scrape.url, None)
        self.assertIn("Senior Engineer", scrape.job_content)
        self.assertEqual(scrape.created_by, self.user)

        mock_parse.assert_called_once()
        args, kwargs = mock_parse.call_args
        self.assertEqual(args[0], scrape.id)
        self.assertEqual(kwargs.get("user_id"), self.user.id)
        self.assertTrue(kwargs.get("sync"), "from_text must call parse_scrape with sync=True")

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_optional_link_stored(self, mock_parse):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={
                "text": "Some job content",
                "link": "https://example.com/jobs/1",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        scrape = Scrape.objects.get(pk=int(resp.json()["data"]["id"]))
        self.assertEqual(scrape.url, "https://example.com/jobs/1")
        mock_parse.assert_called_once()

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_empty_text_rejected(self, mock_parse):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "   "},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Scrape.objects.count(), 0)
        mock_parse.assert_not_called()

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_missing_text_rejected(self, mock_parse):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        mock_parse.assert_not_called()

    def test_auth_required(self):
        self.client.force_authenticate(user=None)
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "content"},
            format="json",
        )
        self.assertIn(resp.status_code, (401, 403))


@patch("job_hunting.api.views.scrapes.FROM_TEXT_MIN_LEN", 1)
class TestScrapeFromTextDuplicateLink(TestCase):
    """Inbox bug: pasting a URL whose JobPost already exists used to create
    a Scrape and run the full LLM extraction before the dedup check fired —
    a wasted agent call. Now we short-circuit with 409 and let the user
    open the existing post or opt-in to a re-parse with force=true.

    Stubs (thin/empty description) still pass through so parse_scrape can
    upgrade them in place.
    """

    DUPLICATE_LINK = "https://talent.toptal.com/portal/job/VjEtSm9iLTQ5Mzg4OA"
    LONG_DESC = " ".join(["word"] * 200)

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="paster2", password="pw")
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Toptal")

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_non_stub_duplicate_returns_409_without_creating_scrape(self, mock_parse):
        # Trust-aware short-circuit: 409 only fires when the new push is
        # SAME-or-lower-trust than the existing post. The from-text
        # endpoint defaults source="paste"; set existing.source="paste"
        # so trust ranks tie and the legacy 409 path runs.
        existing = JobPost.objects.create(
            title="Senior Engineer",
            company=self.company,
            description=self.LONG_DESC,
            link=self.DUPLICATE_LINK,
            source="paste",
            created_by=self.user,
        )
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Senior Engineer at Toptal", "link": self.DUPLICATE_LINK},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        body = resp.json()
        err = body["errors"][0]
        self.assertEqual(err["code"], "duplicate_job_post")
        self.assertEqual(err["meta"]["job_post_id"], existing.id)
        self.assertEqual(err["meta"]["link"], self.DUPLICATE_LINK)
        self.assertEqual(err["meta"]["title"], "Senior Engineer")
        self.assertEqual(err["meta"]["company_name"], "Toptal")
        # No Scrape created, no agent call.
        self.assertEqual(Scrape.objects.count(), 0)
        mock_parse.assert_not_called()

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_stub_duplicate_passes_through_for_upgrade(self, mock_parse):
        """Thin/empty description = stub. parse_scrape's existing
        updated_stub branch should still get a chance to fill it in."""
        JobPost.objects.create(
            title="Senior Engineer",
            company=self.company,
            description="",  # stub
            link=self.DUPLICATE_LINK,
            created_by=self.user,
        )
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Senior Engineer at Toptal", "link": self.DUPLICATE_LINK},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(Scrape.objects.count(), 1)
        mock_parse.assert_called_once()

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_force_true_overrides_dedup(self, mock_parse):
        """User explicitly opts in to re-parse over an existing post."""
        JobPost.objects.create(
            title="Senior Engineer",
            company=self.company,
            description=self.LONG_DESC,
            link=self.DUPLICATE_LINK,
            created_by=self.user,
        )
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={
                "text": "Senior Engineer at Toptal",
                "link": self.DUPLICATE_LINK,
                "force": True,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(Scrape.objects.count(), 1)
        mock_parse.assert_called_once()

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_no_link_skips_dedup_check(self, mock_parse):
        """Paste without a URL — never had a chance to match anything."""
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Senior Engineer at Toptal"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        mock_parse.assert_called_once()

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_extension_push_bypasses_409_on_email_post(self, mock_parse):
        """The whole point of the extension-as-source-of-truth feature:
        when the extension push has a higher source trust than the
        existing post, /scrapes/from-text/ does NOT 409. Instead it
        passes through and lets parse_scrape do the trust-aware
        overwrite. Without this, the 409 short-circuit would block the
        cc_auto self-heal path before it ever ran."""
        JobPost.objects.create(
            title="Wrong Email-Sourced Title",
            company=self.company,
            description=self.LONG_DESC,
            link=self.DUPLICATE_LINK,
            source="email",
            created_by=self.user,
        )
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={
                "text": "Senior Engineer at Toptal",
                "link": self.DUPLICATE_LINK,
                "source": "extension",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(Scrape.objects.count(), 1)
        scrape = Scrape.objects.first()
        self.assertEqual(scrape.source, "extension")
        mock_parse.assert_called_once()


@patch("job_hunting.api.views.scrapes.FROM_TEXT_MIN_LEN", 1)
class TestScrapeFromTextSourceHint(TestCase):
    """The browser extension submits with source='extension' so analytics
    can distinguish web-paste, email-pipeline, and extension-driven
    JobPosts. Endpoint defaults to 'paste' so the existing web form
    keeps working without any frontend change."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="srcuser", password="pw")
        self.client.force_authenticate(user=self.user)

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_default_source_is_paste(self, _mock_parse):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Some content"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        scrape = Scrape.objects.get(pk=int(resp.json()["data"]["id"]))
        self.assertEqual(scrape.source, "paste")

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_extension_source_persists(self, _mock_parse):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Content", "source": "extension"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        scrape = Scrape.objects.get(pk=int(resp.json()["data"]["id"]))
        self.assertEqual(scrape.source, "extension")

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_email_source_persists(self, _mock_parse):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Content", "source": "email"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        scrape = Scrape.objects.get(pk=int(resp.json()["data"]["id"]))
        self.assertEqual(scrape.source, "email")

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_source_normalized_to_lowercase(self, _mock_parse):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Content", "source": "Extension"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        scrape = Scrape.objects.get(pk=int(resp.json()["data"]["id"]))
        self.assertEqual(scrape.source, "extension")

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_invalid_source_falls_back_to_paste(self, _mock_parse):
        # Whitespace / special chars / overlong values must not crash —
        # provenance attribution should never block ingestion.
        for bad in [
            "spaces in here",
            "punct!",
            "x" * 100,
            "../etc/passwd",
            "; DROP TABLE",
        ]:
            with self.subTest(source=bad):
                resp = self.client.post(
                    "/api/v1/scrapes/from-text/",
                    data={"text": "c", "source": bad},
                    format="json",
                )
                self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
                scrape = Scrape.objects.get(pk=int(resp.json()["data"]["id"]))
                self.assertEqual(scrape.source, "paste")
                Scrape.objects.all().delete()

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_blank_source_falls_back_to_paste(self, _mock_parse):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "c", "source": "   "},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        scrape = Scrape.objects.get(pk=int(resp.json()["data"]["id"]))
        self.assertEqual(scrape.source, "paste")


class TestScrapeFromTextPolicy(TestCase):
    """Phase 0 ingest defenses — see plan dazzling-stirring-church.md.

    Hard-rejects URLs and text payloads that don't make sense, before we
    spend an LLM call on them.
    """

    LONG_TEXT = "Senior Engineer at Acme. Remote. " * 10  # ~330 chars

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="policy", password="pw")
        self.client.force_authenticate(user=self.user)

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_rejects_self_host_link(self, mock_parse):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={
                "text": self.LONG_TEXT,
                "link": "https://careercaddy.online/dashboard",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(
            resp.json()["errors"][0]["code"], "blocked_self"
        )
        self.assertEqual(Scrape.objects.count(), 0)
        mock_parse.assert_not_called()

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_rejects_javascript_scheme_link(self, mock_parse):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": self.LONG_TEXT, "link": "javascript:alert(1)"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(resp.json()["errors"][0]["code"], "blocked_scheme")
        mock_parse.assert_not_called()

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_rejects_localhost_link(self, mock_parse):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": self.LONG_TEXT, "link": "http://localhost:4200/x"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(resp.json()["errors"][0]["code"], "blocked_private")
        mock_parse.assert_not_called()

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_short_text_rejected(self, mock_parse):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "too short for a real posting"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.json()["errors"][0]["code"], "text_too_short")
        mock_parse.assert_not_called()

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_oversize_text_rejected(self, mock_parse):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "x" * 600_000},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(resp.json()["errors"][0]["code"], "text_too_long")
        mock_parse.assert_not_called()

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_happy_path_with_public_link_passes(self, mock_parse):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": self.LONG_TEXT, "link": "https://jobs.lever.co/acme/1"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        mock_parse.assert_called_once()


@patch("job_hunting.api.views.scrapes.FROM_TEXT_MIN_LEN", 1)
class TestScrapeFromTextSyncExtraction(TestCase):
    """from_text must call parse_scrape with sync=True so the scrape reaches
    a terminal status before the HTTP response is returned. Without this,
    extension-submitted scrapes could get stuck in extracting if the daemon
    thread dies (scrape 273 incident, 2026-05-02)."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="syncuser", password="pw")
        self.client.force_authenticate(user=self.user)

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_parse_scrape_called_with_sync_true(self, mock_parse):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Real job content here", "source": "extension"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        _, kwargs = mock_parse.call_args
        self.assertTrue(
            kwargs.get("sync"),
            "from_text must invoke parse_scrape(..., sync=True) to prevent stuck-extracting scrapes",
        )

    @patch("job_hunting.lib.parsers.job_post_extractor.JobPostExtractor.analyze_with_ai")
    def test_scrape_is_terminal_when_response_returns(self, mock_analyze):
        """With sync=True, parse_scrape runs inline. The scrape must be in
        a terminal status (completed or failed) before the 202 is returned,
        so the extension's first poll always sees a result."""
        from job_hunting.lib.parsers.job_post_extractor import ParsedJobData

        mock_analyze.return_value = ParsedJobData(
            title="Staff Engineer",
            company_name="SyncCorp",
            description="Build reliable distributed systems at scale. " * 5,
        )
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Staff Engineer at SyncCorp. " * 30, "source": "extension"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        scrape_id = int(resp.json()["data"]["id"])
        scrape = Scrape.objects.get(pk=scrape_id)
        self.assertIn(
            scrape.status,
            ("completed", "failed"),
            f"Scrape {scrape_id} must be terminal after sync=True parse, got {scrape.status!r}",
        )


@patch("job_hunting.api.views.scrapes.FROM_TEXT_MIN_LEN", 1)
class TestScrapeFromTextAutoScore(TestCase):
    """from_text fires auto-scoring after a successful sync parse."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="autoscorer", password="pw")
        self.client.force_authenticate(user=self.user)

    @patch("job_hunting.api.views.scores._auto_score_job_post")
    @patch("job_hunting.lib.parsers.job_post_extractor.JobPostExtractor.analyze_with_ai")
    def test_auto_score_fired_after_successful_parse(self, mock_analyze, mock_auto_score):
        from job_hunting.lib.parsers.job_post_extractor import ParsedJobData
        from job_hunting.models import Company

        Company.objects.create(name="AutoCorp")
        mock_analyze.return_value = ParsedJobData(
            title="Engineer",
            company_name="AutoCorp",
            description="Build things reliably at scale. " * 5,
        )
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Engineer at AutoCorp. " * 20},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        mock_auto_score.assert_called_once()
        _, call_kwargs = mock_auto_score.call_args
        # user_id is the second positional arg
        self.assertEqual(mock_auto_score.call_args[0][1], self.user.id)

    @patch("job_hunting.api.views.scores._auto_score_job_post")
    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_auto_score_not_fired_when_parse_creates_no_job_post(self, mock_parse, mock_auto_score):
        """parse_scrape no-op → scrape has no job_post_id → no Score attempt."""
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "content"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        mock_auto_score.assert_not_called()

    @patch("job_hunting.api.views.scores._auto_score_job_post")
    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_auto_score_error_does_not_break_response(self, mock_parse, mock_auto_score):
        """A scoring error must never surface to the from-text caller."""
        mock_auto_score.side_effect = Exception("boom")
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "content"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
