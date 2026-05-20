"""Tests for POST /api/v1/scrapes/from-text/ — the paste-to-JobPost endpoint."""

from unittest.mock import patch

from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import Company, JobPost, Scrape, ScrapeProfile
from job_hunting.models.job_post_dedupe import _profile_url_rewrites_for_host

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
    def test_incomplete_duplicate_passes_through_for_upgrade(self, mock_parse):
        """JP flagged complete=False bypasses the 409 — same trust rank
        and a long description don't matter, only the explicit flag.
        Replaces the old word-count-based stub bypass."""
        JobPost.objects.create(
            title="Senior Engineer",
            company=self.company,
            description=self.LONG_DESC,
            link=self.DUPLICATE_LINK,
            source="paste",
            complete=False,
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
    def test_409_fires_when_complete_and_stub_share_canonical_link(self, mock_parse):
        """Regression: link is unique, canonical_link is not. If a stub
        /comm/ row and a complete /jobs/view/ row both canonicalize to
        the same URL, an unordered .first() can pick the stub (whose
        complete=False short-circuits the 409) and silently let a
        duplicate scrape through. The dedup query must order
        complete=True first so the gate sees the post the user cares
        about. Mirrors the JP 1532 / scrape 414 incident (2026-05-13)."""
        # Both rows share canonical_link (linkedin url_rewrite collapses
        # /comm/jobs/view/ → /jobs/view/), but only one is complete.
        _profile_url_rewrites_for_host.cache_clear()
        ScrapeProfile.objects.update_or_create(
            hostname="linkedin.com",
            defaults={"url_rewrites": [{
                "match": r"^https?://www\.linkedin\.com/comm/jobs/view/",
                "rewrite": "https://www.linkedin.com/jobs/view/",
            }]},
        )
        try:
            stub = JobPost.objects.create(
                title="Stub Title",
                company=self.company,
                description="",
                link="https://www.linkedin.com/comm/jobs/view/4386478229/",
                source="email",
                complete=False,
                created_by=self.user,
            )
            complete_post = JobPost.objects.create(
                title="Software Engineer II, Security",
                company=self.company,
                description=self.LONG_DESC,
                link="https://www.linkedin.com/jobs/view/4386478229/",
                source="extension",
                complete=True,
                created_by=self.user,
            )
            resp = self.client.post(
                "/api/v1/scrapes/from-text/",
                data={
                    "text": "Software Engineer II, Security at GitHub",
                    "link": "https://www.linkedin.com/jobs/view/4386478229/",
                    "source": "extension",
                },
                format="json",
            )
            self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
            err = resp.json()["errors"][0]
            self.assertEqual(err["meta"]["job_post_id"], complete_post.id)
            self.assertNotEqual(err["meta"]["job_post_id"], stub.id)
            self.assertEqual(Scrape.objects.count(), 0)
            mock_parse.assert_not_called()
        finally:
            _profile_url_rewrites_for_host.cache_clear()

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_409_fires_when_existing_is_extension_complete_and_closed(self, mock_parse):
        """Regression: scrape 414 / JP 1532 (linkedin GitHub Software
        Engineer II, Security, link
        https://www.linkedin.com/jobs/view/4386478229/) on 2026-05-13.
        JP was complete=True, source=extension, posting_status=closed —
        a fresh same-source extension push for the same link slipped
        through and created scrape 414 instead of 409'ing. The 409 path
        only checks complete + trust — posting_status should not affect
        it, since trust ranks tie (extension <= extension) and complete
        is True. Verify the gate fires."""
        existing = JobPost.objects.create(
            title="Senior Engineer",
            company=self.company,
            description=self.LONG_DESC,
            link=self.DUPLICATE_LINK,
            source="extension",
            posting_status="closed",
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
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        body = resp.json()
        err = body["errors"][0]
        self.assertEqual(err["code"], "duplicate_job_post")
        self.assertEqual(err["meta"]["job_post_id"], existing.id)
        self.assertEqual(Scrape.objects.count(), 0)
        mock_parse.assert_not_called()

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
class TestScrapeFromTextZipRecruiterDedupe(TestCase):
    """ZipRecruiter serves the same job at three URL shapes — list-page card
    with -<8hex>/-<8hex> tokens, reposted card without tokens, and email
    redirect /km/<opaque>. Without canonicalization, three submissions of
    the same role create three JobPost rows. This test covers the first
    two shapes; the /km/ case needs upstream redirect resolution and is
    tracked separately."""

    TOKENIZED = (
        "https://www.ziprecruiter.com"
        "/jobs/altus-llc-2287a4f5/software-developer-c-remote-2ffd5d4c"
        "?lk=ABC&tsid=XYZ"
    )
    CLEAN = (
        "https://www.ziprecruiter.com"
        "/jobs/altus-llc/software-developer-c-remote"
        "?lvk=ABC"
    )
    LONG_DESC = " ".join(["word"] * 200)

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="zr", password="pw")
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Altus")
        _profile_url_rewrites_for_host.cache_clear()
        ScrapeProfile.objects.update_or_create(
            hostname="ziprecruiter.com",
            defaults={"url_rewrites": [{
                "match": r"/jobs/([^/]+?)-([a-f0-9]{8})/([^/?#]+?)-([a-f0-9]{8})(?=[/?#]|$)",
                "rewrite": r"/jobs/\1/\3",
            }]},
        )

    def tearDown(self):
        _profile_url_rewrites_for_host.cache_clear()

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_clean_variant_409s_against_tokenized_existing(self, mock_parse):
        existing = JobPost.objects.create(
            title="Software Developer C++",
            company=self.company,
            description=self.LONG_DESC,
            link=self.TOKENIZED,
            source="paste",
            created_by=self.user,
        )
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Software Developer C++ at Altus", "link": self.CLEAN},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        err = resp.json()["errors"][0]
        self.assertEqual(err["code"], "duplicate_job_post")
        self.assertEqual(err["meta"]["job_post_id"], existing.id)
        self.assertEqual(Scrape.objects.count(), 0)
        mock_parse.assert_not_called()


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


@patch("job_hunting.api.views.scrapes.FROM_TEXT_MIN_LEN", 1)
class TestScrapeFromTextExtensionHints(TestCase):
    """Cross-platform dedup signals captured by the browser extension at
    submit time. apply_url is the canonical cross-platform link — written
    directly to JobPost.apply_url. referrer_url is a per-submit signal
    persisted on the Scrape (referrer leg keeps stub creation).
    canonical_redirect picks the ATS JP over the jobboard JP when both
    sides of the pair exist.
    """

    LINKEDIN = "https://www.linkedin.com/jobs/view/4400000000/"
    ATS = "https://ats.rippling.com/rippling/jobs/ebc7a777-aa35-4333-ac95-ebc98e375f75"
    LONG_DESC = " ".join(["word"] * 50)

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="hintuser", password="pw")
        self.client.force_authenticate(user=self.user)

    def _attach_jp_side_effect(self, jp):
        """Side effect for parse_scrape mock — wires the scrape to a
        pre-created JP, mirroring what the real extractor does on a link
        match.  Lets tests assert on JobPost.apply_url after the
        view writes it post-parse."""
        def _se(scrape_id, *args, **kwargs):
            Scrape.objects.filter(pk=scrape_id).update(
                job_post=jp, status="completed"
            )
        return _se

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_apply_url_written_to_jobpost(self, mock_parse):
        company = Company.objects.create(name="Acme")
        jp = JobPost.objects.create(
            title="Senior Engineer",
            company=company,
            description=self.LONG_DESC,
            link=self.LINKEDIN,
            source="extension",
            created_by=self.user,
            complete=False,
        )
        mock_parse.side_effect = self._attach_jp_side_effect(jp)
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={
                "text": "Senior Engineer at Acme",
                "link": self.LINKEDIN,
                "apply_url": self.ATS,
                "referrer_url": "https://www.linkedin.com/jobs/search/",
                "source": "extension",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        jp.refresh_from_db()
        self.assertEqual(jp.apply_url, self.ATS)
        self.assertEqual(jp.apply_url_status, "resolved")
        scrape = Scrape.objects.get(pk=int(resp.json()["data"]["id"]))
        self.assertEqual(scrape.referrer_url, "https://www.linkedin.com/jobs/search/")

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_canonical_link_hint_replaces_submitted_link(self, _mock_parse):
        """LinkedIn's <meta og:url> is preferred over location.href so
        the persisted Scrape.url and downstream JobPost.link land on the
        clean canonical address, not a tracker-laden one."""
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={
                "text": "Senior Engineer at Acme",
                "link": "https://www.linkedin.com/jobs/view/4400000000/?trk=tracking_param",
                "canonical_link_hint": self.LINKEDIN,
                "source": "extension",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        scrape = Scrape.objects.get(pk=int(resp.json()["data"]["id"]))
        self.assertEqual(scrape.url, self.LINKEDIN)

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_apply_url_matches_existing_jp(self, _mock_parse):
        """When a JobPost already exists at the apply-button destination,
        the response surfaces its id so the frontend can render the
        relationship immediately. ATS host → canonical_redirect routes
        there over the submitted LinkedIn JP."""
        company = Company.objects.create(name="Rippling")
        existing = JobPost.objects.create(
            title="Senior Engineer",
            company=company,
            description=self.LONG_DESC,
            link=self.ATS,
            source="extension",
            created_by=self.user,
        )
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={
                "text": "Senior Engineer at Rippling",
                "link": self.LINKEDIN,
                "apply_url": self.ATS,
                "source": "extension",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        meta = resp.json().get("meta") or {}
        self.assertEqual(meta.get("apply_match"), existing.id)
        self.assertEqual(meta.get("canonical_redirect"), existing.id)

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_apply_url_does_not_create_stub(self, _mock_parse):
        """One-channel collapse: when no JP exists at the apply URL the
        api does NOT create a stub. The relationship is fully captured by
        JobPost.apply_url; the second-side JP only materializes when
        someone actually submits it."""
        before = JobPost.objects.count()
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={
                "text": "Senior Engineer at Rippling",
                "link": self.LINKEDIN,
                "apply_url": self.ATS,
                "source": "extension",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        meta = resp.json().get("meta") or {}
        self.assertIsNone(meta.get("apply_match"))
        # No stub at the ATS URL.
        self.assertFalse(JobPost.objects.filter(link=self.ATS).exists())
        # No proliferation of phantom JPs either.
        self.assertEqual(JobPost.objects.count(), before)

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_referrer_creates_stub_with_referrer_source(self, _mock_parse):
        """Symmetric case: ccsend FROM an ATS page after clicking through
        LinkedIn forwards document.referrer. No LinkedIn JP yet → stub
        with source='referrer_stub'. (Referrer leg still creates stubs;
        only the apply leg was collapsed.)"""
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={
                "text": "Senior Engineer at Rippling",
                "link": self.ATS,
                "referrer_url": self.LINKEDIN,
                "source": "extension",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        meta = resp.json().get("meta") or {}
        stub_id = meta.get("referrer_match")
        self.assertIsNotNone(stub_id)
        stub = JobPost.objects.get(pk=stub_id)
        self.assertEqual(stub.link, self.LINKEDIN)
        self.assertEqual(stub.source, "referrer_stub")
        self.assertFalse(stub.complete)

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_canonical_redirect_falls_back_to_submitted_when_no_apply_match(self, _mock_parse):
        """Without an apply URL or when the apply match isn't an ATS
        host, canonical_redirect points at whatever JP the submit
        produced (which may be None when parse_scrape doesn't attach
        one, but the field is still set explicitly)."""
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={
                "text": "Senior Engineer at Rippling",
                "link": self.LINKEDIN,
                "source": "extension",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        meta = resp.json().get("meta") or {}
        self.assertIn("canonical_redirect", meta)

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_private_apply_url_silently_dropped(self, mock_parse):
        """Adversarial private/localhost URLs must not block ingestion
        nor end up persisted as the JP apply_url."""
        company = Company.objects.create(name="Acme")
        jp = JobPost.objects.create(
            title="Senior Engineer",
            company=company,
            description=self.LONG_DESC,
            link=self.LINKEDIN,
            source="extension",
            created_by=self.user,
            complete=False,
        )
        mock_parse.side_effect = self._attach_jp_side_effect(jp)
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={
                "text": "Senior Engineer at Acme",
                "link": self.LINKEDIN,
                "apply_url": "http://localhost:8000/secrets",
                "referrer_url": "http://192.168.1.1/admin",
                "source": "extension",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        jp.refresh_from_db()
        self.assertIsNone(jp.apply_url)
        scrape = Scrape.objects.get(pk=int(resp.json()["data"]["id"]))
        self.assertIsNone(scrape.referrer_url)
        meta = resp.json().get("meta") or {}
        self.assertIsNone(meta.get("apply_match"))
        self.assertIsNone(meta.get("referrer_match"))

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_no_hints_behaves_as_before(self, _mock_parse):
        """Absence of all three hint fields is the existing-behavior
        baseline — endpoint stays backward-compatible."""
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Senior Engineer at Acme", "link": self.LINKEDIN},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        scrape = Scrape.objects.get(pk=int(resp.json()["data"]["id"]))
        self.assertIsNone(scrape.referrer_url)
        meta = resp.json().get("meta") or {}
        self.assertIsNone(meta.get("apply_match"))
        self.assertIsNone(meta.get("referrer_match"))

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_apply_hint_match_via_canonical_link(self, _mock_parse):
        """An existing JP whose canonical_link matches the hint URL (via
        the host's url_rewrites) still resolves — not just exact link
        equality. Mirrors the LinkedIn /comm/ → /jobs/view/ rewrite."""
        _profile_url_rewrites_for_host.cache_clear()
        ScrapeProfile.objects.update_or_create(
            hostname="linkedin.com",
            defaults={"url_rewrites": [{
                "match": r"^https?://www\.linkedin\.com/comm/jobs/view/",
                "rewrite": "https://www.linkedin.com/jobs/view/",
            }]},
        )
        try:
            company = Company.objects.create(name="LinkedHQ")
            existing = JobPost.objects.create(
                title="Senior Engineer",
                company=company,
                description=self.LONG_DESC,
                link=self.LINKEDIN,
                source="extension",
                created_by=self.user,
            )
            resp = self.client.post(
                "/api/v1/scrapes/from-text/",
                data={
                    "text": "Senior Engineer at LinkedHQ",
                    "link": self.ATS,
                    "referrer_url": "https://www.linkedin.com/comm/jobs/view/4400000000/",
                    "source": "extension",
                },
                format="json",
            )
            self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
            meta = resp.json().get("meta") or {}
            self.assertEqual(meta.get("referrer_match"), existing.id)
        finally:
            _profile_url_rewrites_for_host.cache_clear()
