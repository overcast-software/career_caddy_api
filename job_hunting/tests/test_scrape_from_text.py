"""Tests for POST /api/v1/scrapes/from-text/ — the paste-to-JobPost endpoint.

from_text used to run the LLM parse synchronously inside the HTTP request
(``parse_scrape(..., sync=True)``). That in-request allocation was the
CC-122 OOM root cause: it tipped the api cgroup over its 768m mem_limit
and the gunicorn worker got SIGKILLed mid-parse, silently orphaning the
scrape at status='pending'. The durable fix offloads the parse to the
django-q2 qcluster worker — the endpoint now *enqueues*
``job_hunting.lib.tasks.parse_scrape_job`` and returns 202 immediately;
both clients (the MV3 extension + the Ember app) already poll the scrape
to terminal, so the JobPost materializes on a later poll.

Test strategy: ``Q_CLUSTER['sync']`` is NOT on globally under TESTING, so a
real ``async_task`` enqueues an OrmQ row that nothing drains. Endpoint
tests mock ``job_hunting.api.views.scrapes.async_task`` and assert the
enqueue (target + args) + the 202 shape, with the scrape left ``pending``
(no synchronous parse). Tests that need the post-parse effects
(apply_url stamp, auto-score, failure_reason) call ``_run_enqueued_task``
to run the worker leg in-band, exactly as the qcluster worker would.
"""

from unittest.mock import patch

from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import Company, JobPost, Scrape, ScrapeProfile
from job_hunting.models.job_post_dedupe import _profile_url_rewrites_for_host

User = get_user_model()

PARSE_TASK = "job_hunting.lib.tasks.parse_scrape_job"


def _run_enqueued_task(mock_async):
    """Run the parse task the view enqueued, exactly as the qcluster worker
    would. The view dispatches the parse via
    ``async_task("job_hunting.lib.tasks.parse_scrape_job", scrape_id, **kw)``;
    endpoint tests mock ``async_task`` to keep the request fast +
    deterministic, then call this to execute the worker leg in-band and
    assert post-parse effects.
    """
    from job_hunting.lib.tasks import parse_scrape_job

    mock_async.assert_called_once()
    args, kwargs = mock_async.call_args
    assert args[0] == PARSE_TASK, f"expected enqueue of {PARSE_TASK}, got {args[0]!r}"
    return parse_scrape_job(*args[1:], **kwargs)


# Existing tests predate the Phase 0 length floor; relax it so they keep
# focusing on what they were written to test. Length bounds get their own
# coverage in TestScrapeFromTextPolicy below.
@patch("job_hunting.api.views.scrapes.FROM_TEXT_MIN_LEN", 1)
class TestScrapeFromText(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="paster", password="pw")
        self.client.force_authenticate(user=self.user)

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_happy_path_enqueues_parse_returns_202(self, mock_async):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Senior Engineer at Acme. Remote. $180k-$220k."},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        body = resp.json()
        self.assertIn("data", body)
        self.assertEqual(body["data"]["type"], "scrape")

        scrape = Scrape.objects.get(pk=body["data"]["id"])
        self.assertEqual(scrape.url, None)
        self.assertIn("Senior Engineer", scrape.job_content)
        self.assertEqual(scrape.created_by, self.user)

        # The parse is offloaded to the qcluster worker — it does NOT run
        # inline. The scrape stays pending until the worker drains it.
        self.assertEqual(scrape.status, "pending")
        self.assertIsNone(scrape.job_post_id)

        mock_async.assert_called_once()
        args, kwargs = mock_async.call_args
        self.assertEqual(args[0], PARSE_TASK)
        self.assertEqual(args[1], scrape.id)
        self.assertEqual(kwargs.get("user_id"), self.user.id)

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_optional_link_stored(self, mock_async):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={
                "text": "Some job content",
                "link": "https://example.com/jobs/1",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(scrape.url, "https://example.com/jobs/1")
        mock_async.assert_called_once()

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_empty_text_rejected(self, mock_async):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "   "},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Scrape.objects.count(), 0)
        mock_async.assert_not_called()

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_missing_text_rejected(self, mock_async):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        mock_async.assert_not_called()

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_auth_required(self, mock_async):
        self.client.force_authenticate(user=None)
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "content"},
            format="json",
        )
        self.assertIn(resp.status_code, (401, 403))
        mock_async.assert_not_called()


@patch("job_hunting.api.views.scrapes.FROM_TEXT_MIN_LEN", 1)
class TestScrapeFromTextResponseShape(TestCase):
    """Pin the JSON:API response envelope on every 2xx return path.

    The cc_sender browser extension popup reads ``body.data.id`` (the
    scrape id) and ``body.data.relationships['job-post'].data.id``
    immediately after a 202. data.id must always be present. With the
    parse now deferred to the worker, the job-post linkage is ALWAYS the
    empty form (data=None) at 202 time — the JP doesn't exist yet — so the
    popup falls through to its existing background poll. The linkage block
    (data=None + links) must still be present so the popup's optional
    chaining returns null instead of throwing on a missing key path.
    """

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="shape", password="pw")
        self.client.force_authenticate(user=self.user)

    def _assert_envelope(self, body, *, expect_meta=True):
        """Single source of truth for the popup-side contract."""
        self.assertIn("data", body, "envelope missing top-level 'data' key")
        self.assertIsInstance(body["data"], dict, "'data' must be an object, not null/array")
        data = body["data"]
        self.assertEqual(data.get("type"), "scrape", "data.type must be 'scrape'")
        scrape_id = data.get("id")
        self.assertIsInstance(scrape_id, str, "data.id must be a string per JSON:API")
        self.assertTrue(scrape_id, "data.id must be non-empty (popup reads body.data.id)")
        self.assertRegex(
            scrape_id,
            r"^[0-9A-Za-z]{10}$",
            f"data.id must be a 10-char NanoID string, got {scrape_id!r}",
        )
        self.assertIn("attributes", data, "data.attributes block required")
        self.assertIn("relationships", data, "data.relationships block required")
        rels = data["relationships"]
        # The popup reads body.data.relationships['job-post'].data.id —
        # even when no JP is linked yet, the linkage block (data=None +
        # links) MUST exist so optional-chaining returns null instead of
        # throwing on a missing key path.
        self.assertIn(
            "job-post", rels,
            "data.relationships['job-post'] block required (cc_sender popup contract)",
        )
        jp_rel = rels["job-post"]
        self.assertIsInstance(jp_rel, dict, "job-post relationship must be an object")
        # data may be None when no JP linked; the KEY must be present.
        self.assertIn(
            "data", jp_rel,
            "data.relationships['job-post'].data key required even when None",
        )
        if expect_meta:
            self.assertIn("meta", body, "from-text 2xx must carry meta envelope")
            meta = body["meta"]
            self.assertIn("apply_match", meta)
            self.assertIn("referrer_match", meta)
            self.assertIn("canonical_redirect", meta)
            # Fallback channel for the 2026-06-10 popup bug. If the
            # primary data.id read ever fails (proxy strips body,
            # serializer regression, etc.), the popup can read
            # body.meta.scrape_id and still recover. The contract is
            # that BOTH must agree; the test below pins that.
            self.assertIn(
                "scrape_id", meta,
                "from-text meta must mirror scrape_id (popup fallback channel)",
            )
            self.assertEqual(
                str(meta["scrape_id"]), scrape_id,
                "meta.scrape_id must equal data.id",
            )

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_envelope_has_data_id_on_minimal_paste(self, _mock_async):
        """No link, no hints — Doug's failure shape. body.data.id required."""
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Senior Engineer at Acme. Remote."},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        self._assert_envelope(resp.json())

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_envelope_matches_cc_sender_popup_submission(self, _mock_async):
        """Exact wire shape the cc_sender popup posts on the from-text
        fall-through path (useDirectPost=false): link present, source=
        'extension', auto_score=true, all hint fields explicitly null
        and structured_prefill null. This is the path Doug hit when the
        popup reported 'Sent, but no scrape id returned' against a page
        whose ScrapeProfile selectors yielded no structured prefill.
        """
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={
                "text": "Senior Backend Engineer at Acme. Remote-friendly.",
                "link": "https://jobs.example.com/postings/abc-123",
                "source": "extension",
                "auto_score": True,
                "apply_url": None,
                "canonical_link_hint": None,
                "referrer_url": None,
                "structured_prefill": None,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        self._assert_envelope(resp.json())

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_envelope_intact_when_no_job_post_yet(self, _mock_async):
        """With the parse deferred, the scrape envelope carries data.id but
        no JP linkage. The popup must still pin the scrape so it can show
        'Added' and poll the in-flight scrape to completion.
        """
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Some content the extractor will parse later."},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        body = resp.json()
        self._assert_envelope(body)
        # Confirm the JP linkage is the empty form (data=None) — NOT
        # missing the relationships block entirely.
        self.assertIsNone(
            body["data"]["relationships"]["job-post"]["data"],
            "job-post linkage data should be None when no JP linked yet",
        )

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_envelope_jp_linkage_null_because_parse_deferred(self, mock_async):
        """The behavior change CC-122 introduces: even when the parse will
        succeed, the 202 carries job-post linkage data=None because the
        parse hasn't run yet. The JP materializes only after the worker
        drains the queue; the client picks it up on a subsequent poll.
        """
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Staff Engineer at ShapeCorp. " * 20, "source": "extension"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        body = resp.json()
        self._assert_envelope(body)
        self.assertIsNone(
            body["data"]["relationships"]["job-post"]["data"],
            "JP linkage must be None at 202 time — parse is async now",
        )
        scrape = Scrape.objects.get(pk=body["data"]["id"])
        self.assertEqual(scrape.status, "pending")
        self.assertIsNone(scrape.job_post_id)
        # The enqueue is what carries the work forward.
        mock_async.assert_called_once()

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_envelope_id_matches_db_row(self, _mock_async):
        """body.data.id must be the actual Scrape pk, not some serializer
        artifact. Pins the contract that the popup can round-trip the id
        back through GET /scrapes/:id/ without translation.
        """
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Round-trip content."},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        body = resp.json()
        scrape_id_str = body["data"]["id"]
        # Round-trip: the wire id must be parsable back to an existing pk.
        scrape = Scrape.objects.get(pk=scrape_id_str)
        self.assertEqual(str(scrape.pk), scrape_id_str)


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

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_non_stub_duplicate_returns_409_without_creating_scrape(self, mock_async):
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
        # No Scrape created, no agent call enqueued.
        self.assertEqual(Scrape.objects.count(), 0)
        mock_async.assert_not_called()

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_incomplete_duplicate_passes_through_for_upgrade(self, mock_async):
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
        mock_async.assert_called_once()

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_force_true_overrides_dedup(self, mock_async):
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
        mock_async.assert_called_once()

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_no_link_skips_dedup_check(self, mock_async):
        """Paste without a URL — never had a chance to match anything."""
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Senior Engineer at Toptal"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        mock_async.assert_called_once()

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_409_fires_when_complete_and_stub_share_canonical_link(self, mock_async):
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
            mock_async.assert_not_called()
        finally:
            _profile_url_rewrites_for_host.cache_clear()

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_409_fires_when_existing_is_extension_complete_and_closed(self, mock_async):
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
        mock_async.assert_not_called()

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_extension_push_bypasses_409_on_email_post(self, mock_async):
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
        mock_async.assert_called_once()


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

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_clean_variant_409s_against_tokenized_existing(self, mock_async):
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
        mock_async.assert_not_called()


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

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_default_source_is_paste(self, _mock_async):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Some content"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(scrape.source, "paste")

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_extension_source_persists(self, _mock_async):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Content", "source": "extension"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(scrape.source, "extension")

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_email_source_persists(self, _mock_async):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Content", "source": "email"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(scrape.source, "email")

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_source_normalized_to_lowercase(self, _mock_async):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Content", "source": "Extension"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(scrape.source, "extension")

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_invalid_source_falls_back_to_paste(self, _mock_async):
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
                scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
                self.assertEqual(scrape.source, "paste")
                Scrape.objects.all().delete()

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_blank_source_falls_back_to_paste(self, _mock_async):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "c", "source": "   "},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
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

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_rejects_self_host_link(self, mock_async):
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
        mock_async.assert_not_called()

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_rejects_javascript_scheme_link(self, mock_async):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": self.LONG_TEXT, "link": "javascript:alert(1)"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(resp.json()["errors"][0]["code"], "blocked_scheme")
        mock_async.assert_not_called()

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_rejects_localhost_link(self, mock_async):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": self.LONG_TEXT, "link": "http://localhost:4200/x"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(resp.json()["errors"][0]["code"], "blocked_private")
        mock_async.assert_not_called()

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_short_text_rejected(self, mock_async):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "too short for a real posting"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.json()["errors"][0]["code"], "text_too_short")
        mock_async.assert_not_called()

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_oversize_text_rejected(self, mock_async):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "x" * 600_000},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)
        self.assertEqual(resp.json()["errors"][0]["code"], "text_too_long")
        mock_async.assert_not_called()

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_happy_path_with_public_link_passes(self, mock_async):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": self.LONG_TEXT, "link": "https://jobs.lever.co/acme/1"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        mock_async.assert_called_once()


@patch("job_hunting.api.views.scrapes.FROM_TEXT_MIN_LEN", 1)
class TestScrapeFromTextAsyncDispatch(TestCase):
    """CC-122 durable fix: from_text offloads the LLM parse to the qcluster
    worker instead of running it inline in the request. The in-request
    parse was the OOM root cause — the LLM allocation tipped the api cgroup
    over its 768m mem_limit and the gunicorn worker was SIGKILLed mid-parse,
    silently orphaning the scrape at status='pending'. The endpoint now
    enqueues job_hunting.lib.tasks.parse_scrape_job and returns 202; the
    worker drives the scrape to terminal off the django_q_ormq queue.
    """

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="asyncuser", password="pw")
        self.client.force_authenticate(user=self.user)

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_enqueues_parse_scrape_job_without_inline_parse(self, mock_async):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Real job content here", "source": "extension"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        # No synchronous parse: scrape stays pending, no JobPost yet.
        self.assertEqual(scrape.status, "pending")
        self.assertIsNone(scrape.job_post_id)
        # The parse was handed to the worker queue, not run in-band.
        mock_async.assert_called_once()
        args, kwargs = mock_async.call_args
        self.assertEqual(args[0], PARSE_TASK)
        self.assertEqual(args[1], scrape.id)
        self.assertEqual(kwargs.get("user_id"), self.user.id)
        self.assertIn("apply_url", kwargs)
        self.assertIn("auto_score", kwargs)

    @patch("job_hunting.api.views.scrapes.async_task")
    @patch("job_hunting.lib.parsers.job_post_extractor.JobPostExtractor.analyze_with_ai")
    def test_worker_drives_scrape_to_terminal(self, mock_analyze, mock_async):
        """Running the enqueued task (as the worker would) carries the
        scrape from pending → terminal and links the JobPost. This is the
        leg the extension/web poll observes."""
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
        scrape_id = resp.json()["data"]["id"]

        # Drain the queue: run the worker leg in-band.
        _run_enqueued_task(mock_async)

        scrape = Scrape.objects.get(pk=scrape_id)
        self.assertIn(
            scrape.status,
            ("completed", "failed"),
            f"Scrape {scrape_id} must be terminal after the worker runs, got {scrape.status!r}",
        )
        self.assertIsNotNone(
            scrape.job_post_id,
            "the worker leg must link a JobPost on a successful parse",
        )


@patch("job_hunting.api.views.scrapes.FROM_TEXT_MIN_LEN", 1)
class TestScrapeFromTextAutoScore(TestCase):
    """from_text fires auto-scoring after a successful parse. With the parse
    deferred to the worker, the auto-score now runs inside parse_scrape_job
    (post-parse), not in the request — these tests drive the worker leg."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="autoscorer", password="pw")
        self.client.force_authenticate(user=self.user)

    @patch("job_hunting.api.views.scrapes.async_task")
    @patch("job_hunting.api.views.scores._auto_score_job_post")
    @patch("job_hunting.lib.parsers.job_post_extractor.JobPostExtractor.analyze_with_ai")
    def test_auto_score_fired_after_successful_parse(
        self, mock_analyze, mock_auto_score, mock_async
    ):
        from job_hunting.lib.parsers.job_post_extractor import ParsedJobData

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
        # auto_score defaults to True on the enqueue.
        _, kwargs = mock_async.call_args
        self.assertTrue(kwargs.get("auto_score"))

        _run_enqueued_task(mock_async)

        mock_auto_score.assert_called_once()
        # _auto_score_job_post(job_post_id, user_id) — user_id is 2nd positional.
        self.assertEqual(mock_auto_score.call_args[0][1], self.user.id)

    @patch("job_hunting.api.views.scrapes.async_task")
    @patch("job_hunting.api.views.scores._auto_score_job_post")
    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_auto_score_not_fired_when_parse_creates_no_job_post(
        self, mock_parse, mock_auto_score, mock_async
    ):
        """parse no-op → scrape has no job_post_id → no Score attempt."""
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "content"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        _run_enqueued_task(mock_async)
        mock_auto_score.assert_not_called()


class TestParseScrapeJobPostParse(TestCase):
    """Unit coverage for the post-parse work parse_scrape_job replicates,
    now that it lives in the worker rather than the from_text view: stamp
    the extension-supplied apply_url onto the JobPost, fire auto-scoring,
    and never let an auto-score error escape the task.
    """

    ATS = "https://ats.rippling.com/rippling/jobs/ebc7a777-aa35-4333-ac95-ebc98e375f75"
    LONG_DESC = " ".join(["word"] * 50)

    def setUp(self):
        self.user = User.objects.create_user(username="worker", password="pw")
        self.company = Company.objects.create(name="Acme")

    def _attach_jp_side_effect(self, jp):
        """parse_scrape mock side effect — wire the scrape to a pre-created
        JP and flip it complete, mirroring what the real extractor does on
        a link match."""
        def _se(scrape_id, *args, **kwargs):
            Scrape.objects.filter(pk=scrape_id).update(
                job_post=jp, status="completed"
            )
        return _se

    def _make_scrape(self):
        return Scrape.objects.create(
            job_content="Senior Engineer at Acme",
            status="pending",
            created_by=self.user,
            source="extension",
        )

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_apply_url_stamped_on_job_post_after_parse(self, mock_parse):
        from job_hunting.lib.tasks import parse_scrape_job

        jp = JobPost.objects.create(
            title="Senior Engineer",
            company=self.company,
            description=self.LONG_DESC,
            source="extension",
            created_by=self.user,
            complete=False,
        )
        mock_parse.side_effect = self._attach_jp_side_effect(jp)
        scrape = self._make_scrape()

        parse_scrape_job(scrape.id, user_id=self.user.id, apply_url=self.ATS)

        jp.refresh_from_db()
        self.assertEqual(jp.apply_url, self.ATS)
        self.assertEqual(jp.apply_url_status, "resolved")

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_apply_url_canonicalized_at_write(self, mock_parse):
        """CC-139: parse_scrape_job stamps apply_url via queryset.update(),
        which bypasses JobPost.save(), so the canonicalization has to run in
        the task. A token-polluted apply_url with a matching url_rewrites
        rule must land canonical, not raw."""
        from job_hunting.lib.tasks import parse_scrape_job

        _profile_url_rewrites_for_host.cache_clear()
        ScrapeProfile.objects.update_or_create(
            hostname="ripplehire.com",
            defaults={"url_rewrites": [{
                "match": r"([?&])token=[^&]*",
                "rewrite": r"\1token=",
            }]},
        )
        self.addCleanup(_profile_url_rewrites_for_host.cache_clear)

        jp = JobPost.objects.create(
            title="Senior Engineer",
            company=self.company,
            description=self.LONG_DESC,
            source="extension",
            created_by=self.user,
            complete=False,
        )
        mock_parse.side_effect = self._attach_jp_side_effect(jp)
        scrape = self._make_scrape()

        parse_scrape_job(
            scrape.id,
            user_id=self.user.id,
            apply_url="https://apply.ripplehire.com/j/9?token=SESSION",
        )

        jp.refresh_from_db()
        self.assertEqual(
            jp.apply_url, "https://apply.ripplehire.com/j/9?token="
        )
        self.assertEqual(jp.apply_url_status, "resolved")

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_apply_url_not_stamped_without_job_post(self, mock_parse):
        """parse no-op (no JP linked) → nothing to stamp, no crash."""
        from job_hunting.lib.tasks import parse_scrape_job

        scrape = self._make_scrape()
        result = parse_scrape_job(scrape.id, user_id=self.user.id, apply_url=self.ATS)
        self.assertEqual(result["status"], "completed")
        scrape.refresh_from_db()
        self.assertIsNone(scrape.job_post_id)

    @patch("job_hunting.api.views.scores._auto_score_job_post")
    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_auto_score_fired_post_parse(self, mock_parse, mock_auto_score):
        from job_hunting.lib.tasks import parse_scrape_job

        jp = JobPost.objects.create(
            title="Senior Engineer",
            company=self.company,
            description=self.LONG_DESC,
            source="extension",
            created_by=self.user,
        )
        mock_parse.side_effect = self._attach_jp_side_effect(jp)
        scrape = self._make_scrape()

        parse_scrape_job(scrape.id, user_id=self.user.id, auto_score=True)

        mock_auto_score.assert_called_once_with(jp.id, self.user.id)

    @patch("job_hunting.api.views.scores._auto_score_job_post")
    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_auto_score_not_fired_when_disabled(self, mock_parse, mock_auto_score):
        from job_hunting.lib.tasks import parse_scrape_job

        jp = JobPost.objects.create(
            title="Senior Engineer",
            company=self.company,
            description=self.LONG_DESC,
            source="extension",
            created_by=self.user,
        )
        mock_parse.side_effect = self._attach_jp_side_effect(jp)
        scrape = self._make_scrape()

        parse_scrape_job(scrape.id, user_id=self.user.id, auto_score=False)
        mock_auto_score.assert_not_called()

    @patch("job_hunting.api.views.scores._auto_score_job_post")
    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_auto_score_error_swallowed(self, mock_parse, mock_auto_score):
        """An auto-score failure inside the worker must not propagate — the
        scrape already parsed; scoring is best-effort."""
        from job_hunting.lib.tasks import parse_scrape_job

        jp = JobPost.objects.create(
            title="Senior Engineer",
            company=self.company,
            description=self.LONG_DESC,
            source="extension",
            created_by=self.user,
        )
        mock_parse.side_effect = self._attach_jp_side_effect(jp)
        mock_auto_score.side_effect = Exception("boom")
        scrape = self._make_scrape()

        # Must not raise.
        result = parse_scrape_job(scrape.id, user_id=self.user.id, auto_score=True)
        self.assertEqual(result["status"], "completed")

    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_bare_parse_when_no_post_parse_kwargs(self, mock_parse):
        """Default call (no apply_url / auto_score) re-enters parse_scrape
        with sync=True and skips the post-parse work."""
        from job_hunting.lib.tasks import parse_scrape_job

        scrape = self._make_scrape()
        parse_scrape_job(scrape.id, user_id=self.user.id)
        mock_parse.assert_called_once()
        _, kwargs = mock_parse.call_args
        self.assertTrue(kwargs.get("sync"))


@patch("job_hunting.api.views.scrapes.FROM_TEXT_MIN_LEN", 1)
class TestScrapeFromTextExtensionHints(TestCase):
    """Cross-platform dedup signals captured by the browser extension at
    submit time. apply_url is the canonical cross-platform link — written
    to JobPost.apply_url by the worker after the parse. referrer_url is a
    per-submit signal persisted on the Scrape (referrer leg keeps stub
    creation, computed in the request). canonical_redirect picks the ATS JP
    over the jobboard JP when both sides of the pair exist.
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
        match.  Lets tests assert on JobPost.apply_url after the worker
        writes it post-parse."""
        def _se(scrape_id, *args, **kwargs):
            Scrape.objects.filter(pk=scrape_id).update(
                job_post=jp, status="completed"
            )
        return _se

    @patch("job_hunting.api.views.scrapes.async_task")
    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_apply_url_written_to_jobpost(self, mock_parse, mock_async):
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
        # referrer_url is persisted on the Scrape in the request.
        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(scrape.referrer_url, "https://www.linkedin.com/jobs/search/")
        # apply_url lands on the JobPost only after the worker runs.
        _run_enqueued_task(mock_async)
        jp.refresh_from_db()
        self.assertEqual(jp.apply_url, self.ATS)
        self.assertEqual(jp.apply_url_status, "resolved")

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_canonical_link_hint_replaces_submitted_link(self, _mock_async):
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
        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(scrape.url, self.LINKEDIN)

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_apply_url_matches_existing_jp(self, _mock_async):
        """When a JobPost already exists at the apply-button destination,
        the response surfaces its id so the frontend can render the
        relationship immediately. ATS host → canonical_redirect routes
        there over the submitted LinkedIn JP. This meta is computed in the
        request from existing posts, independent of the deferred parse."""
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

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_apply_url_does_not_create_stub(self, _mock_async):
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

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_referrer_creates_stub_with_referrer_source(self, _mock_async):
        """Symmetric case: ccsend FROM an ATS page after clicking through
        LinkedIn forwards document.referrer. No LinkedIn JP yet → stub
        with source='referrer_stub'. (Referrer leg still creates stubs in
        the request; only the apply leg was collapsed.)"""
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

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_canonical_redirect_null_when_no_apply_match(self, _mock_async):
        """Without an apply URL match, canonical_redirect resolves to null:
        the submitted JP doesn't exist yet (async parse), and there is no
        apply-leg match to route to. The key is still present in meta."""
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
        self.assertIsNone(meta.get("canonical_redirect"))

    @patch("job_hunting.api.views.scrapes.async_task")
    @patch("job_hunting.lib.parsers.job_post_extractor.parse_scrape")
    def test_private_apply_url_silently_dropped(self, mock_parse, mock_async):
        """Adversarial private/localhost URLs must not block ingestion
        nor end up persisted as the JP apply_url — even after the worker
        runs with a JobPost linked."""
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
        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertIsNone(scrape.referrer_url)
        meta = resp.json().get("meta") or {}
        self.assertIsNone(meta.get("apply_match"))
        self.assertIsNone(meta.get("referrer_match"))
        # Even with the JobPost linked, the dropped apply_url is not stamped.
        _run_enqueued_task(mock_async)
        jp.refresh_from_db()
        self.assertIsNone(jp.apply_url)

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_no_hints_behaves_as_before(self, _mock_async):
        """Absence of all three hint fields is the existing-behavior
        baseline — endpoint stays backward-compatible."""
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Senior Engineer at Acme", "link": self.LINKEDIN},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertIsNone(scrape.referrer_url)
        meta = resp.json().get("meta") or {}
        self.assertIsNone(meta.get("apply_match"))
        self.assertIsNone(meta.get("referrer_match"))

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_apply_hint_match_via_canonical_link(self, _mock_async):
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


@patch("job_hunting.api.views.scrapes.FROM_TEXT_MIN_LEN", 1)
class TestScrapeFromTextStructuredPrefill(TestCase):
    """The extension reads per-host css_selectors.job_data via the
    extension-selectors endpoint, runs each selector against the live
    DOM, and posts the resulting dict as `structured_prefill`. The api
    persists it on Scrape.extension_prefill; JobPostExtractor uses it
    as a $0 LLM-skip when title + company_name are present.
    """

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="prefiller", password="pw")
        self.client.force_authenticate(user=self.user)

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_structured_prefill_persisted_on_scrape(self, _mock_async):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={
                "text": "Senior Engineer at Acme",
                "source": "extension",
                "structured_prefill": {
                    "title": "Senior Engineer",
                    "company_name": "Acme",
                    "location": "Remote",
                },
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(
            scrape.extension_prefill,
            {
                "title": "Senior Engineer",
                "company_name": "Acme",
                "location": "Remote",
            },
        )

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_prefill_non_string_values_dropped(self, _mock_async):
        """Extension bugs / type drift mustn't poison the prefill
        column — non-strings get filtered before the JSONField write."""
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={
                "text": "Senior Engineer at Acme",
                "structured_prefill": {
                    "title": "Senior Engineer",
                    "company_name": "Acme",
                    "salary": 180000,
                    "description": ["nested", "structure"],
                },
            },
            format="json",
        )
        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(
            scrape.extension_prefill,
            {"title": "Senior Engineer", "company_name": "Acme"},
        )

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_prefill_blank_values_dropped(self, _mock_async):
        """Whitespace / empty selector misses end up as empty strings;
        strip them so the extractor's title+company_name gate sees
        truthful coverage."""
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={
                "text": "Senior Engineer at Acme",
                "structured_prefill": {
                    "title": "Senior Engineer",
                    "company_name": "   ",
                },
            },
            format="json",
        )
        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(scrape.extension_prefill, {"title": "Senior Engineer"})

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_prefill_missing_yields_null(self, _mock_async):
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Senior Engineer at Acme"},
            format="json",
        )
        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertIsNone(scrape.extension_prefill)

    @patch("job_hunting.api.views.scrapes.async_task")
    def test_prefill_non_dict_silently_ignored(self, _mock_async):
        """A malformed payload (list instead of dict) shouldn't 4xx the
        whole submit — drop the bad signal, persist nothing."""
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={
                "text": "Senior Engineer at Acme",
                "structured_prefill": ["title", "company_name"],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertIsNone(scrape.extension_prefill)


@patch("job_hunting.api.views.scrapes.FROM_TEXT_MIN_LEN", 1)
class TestScrapeFromTextFailureReason(TestCase):
    """Operator-facing diagnostic on post-extract failures.

    Before this change, /scrapes/from-text/ swallowed failures into a
    bare status='failed' badge: the extension popup said "could not
    parse" and the only real surface was the api container log. Add a
    failure_reason column on Scrape, populated at every failed-status
    write site (placeholder rejection in process_evaluation, parser
    exception path in parse_scrape, the safety-net die-before-terminal
    branch). Serializer exposes it read-only.

    The parse now runs in the worker, so these drive the worker leg via
    _run_enqueued_task and then read the terminal Scrape state.
    """

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="diaguser", password="pw")
        self.client.force_authenticate(user=self.user)

    @patch("job_hunting.api.views.scrapes.async_task")
    @patch("job_hunting.lib.parsers.job_post_extractor.JobPostExtractor.analyze_with_ai")
    def test_placeholder_title_writes_failure_reason(self, mock_analyze, mock_async):
        """Stubbed extractor returns a placeholder title — process_evaluation
        sets failure_reason on the row before flipping status=failed."""
        from job_hunting.lib.parsers.job_post_extractor import ParsedJobData

        mock_analyze.return_value = ParsedJobData(
            title="N/A",
            company_name="AcmeCorp",
            description="A real-looking description. " * 20,
        )
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Senior Engineer at AcmeCorp. " * 30, "source": "extension"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        _run_enqueued_task(mock_async)
        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(scrape.status, "failed")
        self.assertIsNotNone(scrape.failure_reason)
        self.assertIn("placeholder title", scrape.failure_reason)
        self.assertIn("N/A", scrape.failure_reason)

    @patch("job_hunting.api.views.scrapes.async_task")
    @patch("job_hunting.lib.parsers.job_post_extractor.JobPostExtractor.analyze_with_ai")
    def test_placeholder_company_writes_failure_reason(self, mock_analyze, mock_async):
        """Placeholder company branch — symmetric to the title branch."""
        from job_hunting.lib.parsers.job_post_extractor import ParsedJobData

        mock_analyze.return_value = ParsedJobData(
            title="Staff Engineer",
            company_name="unknown",
            description="A real-looking description. " * 20,
        )
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Staff Engineer at SomePlace. " * 30, "source": "extension"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        _run_enqueued_task(mock_async)
        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(scrape.status, "failed")
        self.assertIsNotNone(scrape.failure_reason)
        self.assertIn("placeholder company", scrape.failure_reason)
        self.assertIn("unknown", scrape.failure_reason)

    @patch("job_hunting.api.views.scrapes.async_task")
    @patch("job_hunting.lib.parsers.job_post_extractor.JobPostExtractor.analyze_with_ai")
    def test_extractor_exception_writes_failure_reason(self, mock_analyze, mock_async):
        """When parser.parse raises, the caught exception is threaded
        into _log_scrape_status as failure_reason so the operator sees
        the exception repr without reading container logs."""
        mock_analyze.side_effect = RuntimeError("LLM provider exploded")
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Senior Engineer at AcmeCorp. " * 30, "source": "extension"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        _run_enqueued_task(mock_async)
        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(scrape.status, "failed")
        self.assertIsNotNone(scrape.failure_reason)
        self.assertIn("parse_scrape exception", scrape.failure_reason)
        self.assertIn("LLM provider exploded", scrape.failure_reason)

    @patch("job_hunting.api.views.scrapes.async_task")
    @patch("job_hunting.lib.parsers.completeness_reviewer.maybe_review_and_persist")
    @patch("job_hunting.lib.parsers.job_post_extractor.JobPostExtractor.analyze_with_ai")
    def test_success_path_leaves_failure_reason_null(
        self, mock_analyze, mock_review, mock_async
    ):
        """The happy path doesn't touch failure_reason — column stays NULL.
        CompletenessReviewer is mocked out so the test stays focused on the
        failure_reason write semantics, not LLM scoring."""
        from job_hunting.lib.parsers.job_post_extractor import ParsedJobData

        Company.objects.create(name="AutoCorp")
        mock_analyze.return_value = ParsedJobData(
            title="Engineer",
            company_name="AutoCorp",
            description=(
                "Senior backend engineer wanted to design and ship a "
                "distributed event-sourcing platform on Kafka and Postgres. "
                "You'll own service boundaries, schema evolution, and the "
                "on-call rotation. Requirements: 5+ years Python or Go, "
                "production experience with streaming systems, comfort "
                "leading code reviews and architectural decisions. We pay "
                "competitively and offer remote work across US time zones."
            ),
        )
        mock_review.return_value = None  # skip the LLM-side rejection
        resp = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Engineer at AutoCorp. " * 20, "source": "extension"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_202_ACCEPTED)
        _run_enqueued_task(mock_async)
        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(scrape.status, "completed")
        self.assertIsNone(scrape.failure_reason)

    @patch("job_hunting.api.views.scrapes.async_task")
    @patch("job_hunting.lib.parsers.job_post_extractor.JobPostExtractor.analyze_with_ai")
    def test_serializer_exposes_failure_reason(self, mock_analyze, mock_async):
        """GET /scrapes/:id/ returns failure_reason as a JSON:API attribute."""
        mock_analyze.side_effect = RuntimeError("downstream timeout")
        post = self.client.post(
            "/api/v1/scrapes/from-text/",
            data={"text": "Senior Engineer at AcmeCorp. " * 30, "source": "extension"},
            format="json",
        )
        scrape_id = post.json()["data"]["id"]
        _run_enqueued_task(mock_async)

        resp = self.client.get(f"/api/v1/scrapes/{scrape_id}/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        attrs = resp.json()["data"]["attributes"]
        self.assertIn("failure_reason", attrs)
        self.assertIsNotNone(attrs["failure_reason"])
        self.assertIn("downstream timeout", attrs["failure_reason"])

    def test_failure_reason_rejected_on_patch(self):
        """failure_reason is read-only on the wire — clients must not be
        able to overwrite the diagnostic via PATCH."""
        scrape = Scrape.objects.create(
            created_by=self.user,
            status="failed",
            failure_reason="real diagnostic",
        )
        resp = self.client.patch(
            f"/api/v1/scrapes/{scrape.id}/",
            data={
                "data": {
                    "type": "scrape",
                    "id": str(scrape.id),
                    "attributes": {"failure_reason": "client-injected lie"},
                }
            },
            format="json",
        )
        # Either accepted-but-ignored or rejected — both are fine; what
        # matters is the persisted value is unchanged.
        scrape.refresh_from_db()
        self.assertEqual(scrape.failure_reason, "real diagnostic")
        # Document the observed behavior so a future framework upgrade
        # that changes the response code from accepted-but-ignored to
        # rejected surfaces here for review.
        self.assertIn(resp.status_code, (200, 400, 422))
