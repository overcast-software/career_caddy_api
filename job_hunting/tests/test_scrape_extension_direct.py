"""Phase A of the Extension direct-POST plan — api primitives.

Three test classes:

* ``ScrapeSerializerExtensionDirectValidationTests`` covers the
  validate_scrape_source_mode_payload contract on the POST surface.
* ``ScrapeViewSetExtensionDirectCreateTests`` covers the create() flow
  end-to-end: dedupe-first walk, persistence, JP binding.
* ``ExtensionDirectMergeBiasTests`` covers the
  prefer_extension_direct_link rule inside _trust_aware_overwrite.

Why: the extension is the tangible piece for users logging applications.
Phase B's scrape-graph fast path can't ship until this surface enforces
the gate the plan promises. CC-122 relaxed that gate from
title+company+description to `description` ONLY — title/company are
LLM-extracted from the captured innerText on the worker for auth-walled
curated-miss pages (LinkedIn/Toptal). The serializer contract is also
the surface cc_auto's parallel Phase C content-script POSTs against —
getting the rejection-message shape right here matters for the
extension's UX.
"""

from unittest.mock import patch

from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from job_hunting.models import (
    Company,
    JobPost,
    JobPostOverwriteDecision,
    Scrape,
)
from job_hunting.models.job_post_dedupe import prefer_extension_direct_link
from job_hunting.lib.parsers.job_post_extractor import (
    JobPostExtractor,
    ParsedJobData,
    parse_scrape,
)


User = get_user_model()


def _well_formed_payload(**overrides):
    """Minimal extension-direct payload; overridable per test.

    Returns a dict shaped to satisfy the validator's three-required-
    field gate (title + company + description, all non-empty strings).
    """
    payload = {
        "title": "Senior Widget Engineer",
        "company": "Acme Co",
        "description": "Build widgets at scale. " * 5,
    }
    payload.update(overrides)
    return payload


def _curated_payload(
    *,
    title,
    company_name,
    description=None,
    raw_description=None,
    location=None,
    apply_url=None,
):
    """Build an extension-direct captured_payload in the real cc_sender
    wire shape (popup.js Send fast-path / validator createFromProposed):

    - CLEAN per-selector values live under
      ``extraction_hints.structured_prefill`` (title, company_name,
      description, location).
    - The TOP-LEVEL ``description`` is the raw full-page innerText
      (``payload.text`` in the extension) — nav/footer/"page has loaded"
      noise. ``raw_description`` populates it so tests can prove the JP
      description never sources from it.

    The validator requires non-empty top-level title/company/description,
    so the top-level description always falls back to a non-empty value.
    """
    structured = {"title": title, "company_name": company_name}
    if description is not None:
        structured["description"] = description
    if location is not None:
        structured["location"] = location

    top_description = raw_description or description or ("page text " * 20)
    payload = {
        "title": title,
        "company": company_name,
        "description": top_description,
        "extraction_hints": {"structured_prefill": structured},
    }
    if location is not None:
        payload["location"] = location
    if apply_url is not None:
        payload["apply_url"] = apply_url
    return payload


def _post_body(url, *, source_mode=None, captured_payload=None, **extra_attrs):
    """Build a JSON:API scrape-create payload.

    Keeps the source_mode + captured_payload keys absent when None so the
    "no-op write" path tests can exercise the legacy-shape (browser-
    default) behavior without leaking new fields.
    """
    attrs = {"url": url, "status": "hold", **extra_attrs}
    if source_mode is not None:
        attrs["source_mode"] = source_mode
    if captured_payload is not None:
        attrs["captured_payload"] = captured_payload
    return {"data": {"attributes": attrs}}


class ScrapeSerializerExtensionDirectValidationTests(TestCase):
    """POST /api/v1/scrapes/ — source_mode / captured_payload validation.

    Mirrors the EmailForwardSourceTests pattern: rejections are
    400 + ``errors[0].detail`` naming the offending field token so the
    extension can branch on it. Each negative path also asserts the DB
    state — a rejected POST must NOT have minted a scrape row, otherwise
    the extension would silently fork phantom hold scrapes the runner
    later picks up.
    """

    def setUp(self):
        # Scrape POST is staff-gated during alpha (see test_scrape_
        # create_staff_gate.py). The extension Doug installs is keyed
        # to a staff user during the v0.4.x rollout window.
        self.user = User.objects.create_user(
            username="dough", password="p", is_staff=True
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_well_formed_extension_direct_persists_payload(self):
        # Happy path: the contract Doug's extension exercises. Both
        # source_mode and captured_payload land on the row, ready for
        # the Phase B scrape-graph fast-path to consume.
        resp = self.client.post(
            "/api/v1/scrapes/",
            _post_body(
                "https://example.com/jobs/extdirect-happy",
                source_mode="extension-direct",
                captured_payload=_well_formed_payload(
                    apply_url="https://ats.example.com/apply/123",
                    location="Remote (US)",
                    extraction_hints={"selector": ".jobtitle"},
                ),
            ),
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)

        scrape_id = resp.json()["data"]["id"]
        scrape = Scrape.objects.get(pk=scrape_id)
        self.assertEqual(scrape.source_mode, "extension-direct")
        self.assertEqual(
            scrape.captured_payload["title"], "Senior Widget Engineer"
        )
        self.assertEqual(scrape.captured_payload["company"], "Acme Co")
        self.assertIn("Build widgets", scrape.captured_payload["description"])
        # Optional fields round-trip too.
        self.assertEqual(
            scrape.captured_payload["apply_url"],
            "https://ats.example.com/apply/123",
        )
        self.assertEqual(scrape.captured_payload["location"], "Remote (US)")
        self.assertEqual(
            scrape.captured_payload["extraction_hints"],
            {"selector": ".jobtitle"},
        )

    def test_extension_direct_without_payload_rejected(self):
        # Required-field rule — extension-direct without payload is a
        # client bug (extension forgot to attach the capture). Reject
        # at 400 and confirm no scrape was minted.
        resp = self.client.post(
            "/api/v1/scrapes/",
            _post_body(
                "https://example.com/jobs/extdirect-no-payload",
                source_mode="extension-direct",
            ),
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        body = resp.json()
        self.assertIn("captured_payload", body["errors"][0]["detail"])
        # No row leaked.
        self.assertFalse(
            Scrape.objects.filter(
                url="https://example.com/jobs/extdirect-no-payload"
            ).exists()
        )

    def test_extension_direct_missing_description_rejected(self):
        # CC-122 relaxed the gate to `description` ONLY (title/company are
        # LLM-extracted from the captured text on the worker). A capture
        # with NO description is still a useless shell — reject at 400 and
        # confirm no row leaked.
        payload = _well_formed_payload()
        del payload["description"]
        url = "https://example.com/jobs/extdirect-missing-description"
        resp = self.client.post(
            "/api/v1/scrapes/",
            _post_body(
                url,
                source_mode="extension-direct",
                captured_payload=payload,
            ),
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn(
            "captured_payload.description",
            resp.json()["errors"][0]["detail"],
        )
        self.assertFalse(
            Scrape.objects.filter(url=url).exists(),
            "row leaked despite missing description",
        )

    def test_extension_direct_missing_title_company_accepted(self):
        # CC-122 — auth-walled curated-miss: title/company absent is NO
        # LONGER a 400. The capture carries innerText (description) the
        # server can't re-scrape (login wall), so the row is minted and
        # the worker LLM-extracts title/company. Assert acceptance for
        # both title-absent and company-absent shapes.
        for missing in ("title", "company"):
            with self.subTest(missing=missing):
                payload = _well_formed_payload()
                del payload[missing]
                url = f"https://example.com/jobs/extdirect-nofield-{missing}"
                with patch(
                    "job_hunting.api.views.scrapes.async_task"
                ) as mock_async:
                    resp = self.client.post(
                        "/api/v1/scrapes/",
                        _post_body(
                            url,
                            source_mode="extension-direct",
                            captured_payload=payload,
                        ),
                        format="json",
                    )
                self.assertEqual(resp.status_code, 201, resp.content)
                scrape = Scrape.objects.get(url=url)
                # No synchronous JobPost — the parse is enqueued.
                self.assertIsNone(scrape.job_post_id)
                self.assertEqual(scrape.status, "pending")
                mock_async.assert_called_once()
                # Enqueued the SAME worker path from-text uses.
                self.assertEqual(
                    mock_async.call_args.args[0],
                    "job_hunting.lib.tasks.parse_scrape_job",
                )

    def test_extension_direct_empty_string_description_rejected(self):
        # "Trust presence" — empty-string is NOT presence. An extension
        # content-script that renders "" into the description would
        # otherwise mint a shell the worker can't extract anything from.
        payload = _well_formed_payload(description="   ")
        url = "https://example.com/jobs/extdirect-empty-description"
        resp = self.client.post(
            "/api/v1/scrapes/",
            _post_body(
                url,
                source_mode="extension-direct",
                captured_payload=payload,
            ),
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn(
            "captured_payload.description",
            resp.json()["errors"][0]["detail"],
        )
        self.assertFalse(
            Scrape.objects.filter(url=url).exists(),
            "row leaked despite empty description",
        )

    def test_browser_mode_with_payload_rejected(self):
        # Browser-mode writes that carry a payload are almost certainly
        # a client bug echoing a stale field — symmetric with the
        # email-forward / forwarded_via_address defensive shape. Reject
        # so the bug surfaces instead of writing a half-fast-path row.
        resp = self.client.post(
            "/api/v1/scrapes/",
            _post_body(
                "https://example.com/jobs/browser-with-payload",
                source_mode="browser",
                captured_payload=_well_formed_payload(),
            ),
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        body = resp.json()
        self.assertIn("captured_payload", body["errors"][0]["detail"])
        self.assertFalse(
            Scrape.objects.filter(
                url="https://example.com/jobs/browser-with-payload"
            ).exists()
        )

    def test_default_source_mode_is_browser(self):
        # Migration backfill + model default. A legacy POST that doesn't
        # mention source_mode at all gets the browser default — same
        # capture path the historical Camoufox/Playwright runner has
        # always used. No validation fires.
        resp = self.client.post(
            "/api/v1/scrapes/",
            _post_body("https://example.com/jobs/legacy-shape"),
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        scrape = Scrape.objects.get(
            url="https://example.com/jobs/legacy-shape"
        )
        self.assertEqual(scrape.source_mode, "browser")
        self.assertIsNone(scrape.captured_payload)

    def test_unknown_source_mode_rejected(self):
        # The choice set is closed today (browser, extension-direct).
        # Surface unknown values at 400 rather than letting them sneak
        # through to a DB-side CharField choice that Django silently
        # accepts but a future db-level CHECK would reject.
        resp = self.client.post(
            "/api/v1/scrapes/",
            _post_body(
                "https://example.com/jobs/bad-mode",
                source_mode="nonsense",
                captured_payload=_well_formed_payload(),
            ),
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("source_mode", resp.json()["errors"][0]["detail"])


class ScrapeViewSetExtensionDirectCreateTests(TestCase):
    """End-to-end create flow — dedupe-first walk + JP binding."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="dough", password="p", is_staff=True
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Acme")

    def test_extension_direct_bypasses_409_and_overwrites_stale_jp(self):
        """Dedupe-first walk + Phase B synchronous consume:

        The five-clause visibility / dedupe contract still holds —
        canonical_link / fingerprint / sticky-closed run as today —
        but the 409 gate that protects context-free callers (chat,
        bookmarklet) deliberately does NOT fire for extension-direct.
        The captured_payload is fresher than the existing JP's stored
        contents (the user just saw their browser render it), so the
        Phase B consumer builds-or-updates the JobPost synchronously
        right here.

        Existing JP came from the email pipeline (source='email',
        trust 20). The extension push (source='extension', trust 100)
        outranks it, so the trust-aware overwrite flips the post in
        place — title/company/source — and writes a
        JobPostOverwriteDecision audit row. No duplicate JobPost is
        minted; the scrape is linked and left completed (not hold).
        """
        link = "https://example.com/jobs/known-link"
        existing_jp = JobPost.objects.create(
            title="Old Title",
            company=self.company,
            link=link,
            description="x" * 500,
            created_by=self.user,
            source="email",
        )

        resp = self.client.post(
            "/api/v1/scrapes/",
            _post_body(
                link,
                source_mode="extension-direct",
                captured_payload=_curated_payload(
                    title="Senior Widget Engineer",
                    company_name="Acme Co",
                    description="Real curated description. " * 20,
                ),
            ),
            format="json",
        )
        # Mints a new scrape — does NOT 409 the way a browser-mode
        # POST against the same link would.
        self.assertEqual(resp.status_code, 201, resp.content)

        scrape_id = resp.json()["data"]["id"]
        scrape = Scrape.objects.get(pk=scrape_id)
        self.assertEqual(scrape.source_mode, "extension-direct")
        self.assertIsNotNone(scrape.captured_payload)
        # Linked to the existing JP — no duplicate minted.
        self.assertEqual(scrape.job_post_id, existing_jp.id)
        self.assertEqual(JobPost.objects.count(), 1)
        # Not left dangling as a hold the runner would claim.
        self.assertEqual(scrape.status, "completed")
        # Response carries the job-post relationship so the extension can
        # navigate to the post (body.data.relationships['job-post'].data.id).
        rel = resp.json()["data"]["relationships"]["job-post"]["data"]
        self.assertEqual(rel["id"], str(existing_jp.id))
        # Trust-aware overwrite flipped the stale email post in place.
        existing_jp.refresh_from_db()
        self.assertEqual(existing_jp.title, "Senior Widget Engineer")
        self.assertEqual(existing_jp.source, "extension")
        decision = JobPostOverwriteDecision.objects.filter(
            job_post=existing_jp, triggering_scrape=scrape
        ).first()
        self.assertIsNotNone(decision)
        self.assertIn("title", decision.changed_fields)

    def test_extension_direct_creates_jobpost_from_structured_prefill(self):
        """No existing JP: the Phase B consumer creates one synchronously
        from the CLEAN structured_prefill fields, links the scrape, leaves
        it completed, and the JP is immediately findable by filter[link].

        Critically: the JobPost description is the CLEAN
        structured_prefill.description — NOT the raw full-page innerText
        that the extension ships in the top-level captured_payload.description
        (the validator's "Create job-post" path sets that to page text).
        """
        link = "https://example.com/jobs/brand-new-role"
        raw_page_text = (
            "Skip to main content. Cookie banner. Software Engineer | "
            "BoardCo page has loaded. APPLY. Footer nav junk. " * 8
        )
        clean_description = (
            "We are hiring a Software Engineer to build distributed "
            "systems. Responsibilities include design and on-call. " * 6
        )
        resp = self.client.post(
            "/api/v1/scrapes/",
            _post_body(
                link,
                source_mode="extension-direct",
                captured_payload=_curated_payload(
                    title="Software Engineer",
                    company_name="BoardCo",
                    description=clean_description,
                    # Raw innerText at top-level — must NOT leak into the JP.
                    raw_description=raw_page_text,
                    location="Austin, TX",
                ),
            ),
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)

        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(scrape.status, "completed")
        self.assertIsNotNone(scrape.job_post_id)

        jp = JobPost.objects.get(pk=scrape.job_post_id)
        self.assertEqual(jp.title, "Software Engineer")
        self.assertEqual(jp.location, "Austin, TX")
        self.assertEqual(jp.source, "extension")
        # The CLEAN description landed; the raw page noise did NOT.
        self.assertEqual(jp.description, clean_description.strip())
        self.assertNotIn("page has loaded", jp.description or "")
        self.assertNotIn("Cookie banner", jp.description or "")
        # Response relationship + library lookup both resolve the JP.
        rel = resp.json()["data"]["relationships"]["job-post"]["data"]
        self.assertEqual(rel["id"], str(jp.id))

        lookup = self.client.get(f"/api/v1/job-posts/?filter[link]={link}")
        self.assertEqual(lookup.status_code, 200, lookup.content)
        found_ids = {row["id"] for row in lookup.json()["data"]}
        self.assertIn(str(jp.id), found_ids)

    def test_extension_direct_does_not_use_raw_top_level_description(self):
        """Defense-in-depth for the description-source rule: even when the
        payload carries NO structured_prefill.description, the raw
        top-level description (full-page innerText) must not become the
        JobPost description. The post is created with title/company but an
        empty description rather than page chrome."""
        link = "https://example.com/jobs/no-clean-desc"
        raw_page_text = "Nav. Footer. page has loaded. APPLY NOW. " * 20
        payload = _curated_payload(
            title="Data Engineer",
            company_name="PipelineCo",
            description=None,  # no structured_prefill.description
            raw_description=raw_page_text,
        )
        resp = self.client.post(
            "/api/v1/scrapes/",
            _post_body(
                link,
                source_mode="extension-direct",
                captured_payload=payload,
            ),
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        jp = JobPost.objects.get(pk=scrape.job_post_id)
        self.assertEqual(jp.title, "Data Engineer")
        self.assertFalse(jp.description)
        self.assertNotIn("page has loaded", jp.description or "")

    def test_extension_direct_description_only_enqueues_async_parse(self):
        """CC-122 — auth-walled curated-miss (LinkedIn/Toptal): the
        payload carries only the captured innerText (top-level
        description), no resolvable title/company. Instead of failing or
        enqueuing an impossible browser re-scrape, the consumer seeds
        job_content from the captured text and enqueues the SAME async
        worker path /scrapes/from-text/ uses (parse_scrape_job), leaving
        the scrape ``pending`` for the client to poll to terminal.
        """
        link = "https://www.linkedin.com/jobs/view/4437716572/"
        captured_innertext = (
            "Senior Software Engineer at BigCorp. We are hiring engineers "
            "to build distributed systems. Apply now. " * 10
        )
        # No title/company, no structured_prefill — exactly the curated-
        # miss shape the extension sends for an auth-walled page.
        payload = {"description": captured_innertext}

        with patch("job_hunting.api.views.scrapes.async_task") as mock_async:
            resp = self.client.post(
                "/api/v1/scrapes/",
                _post_body(
                    link,
                    source_mode="extension-direct",
                    captured_payload=payload,
                ),
                format="json",
            )
        self.assertEqual(resp.status_code, 201, resp.content)

        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        # Never failed, never a browser hold the runner would claim.
        self.assertEqual(scrape.status, "pending")
        self.assertEqual(scrape.source_mode, "extension-direct")
        # job_content seeded from the captured innerText so the worker
        # LLM-extractor has text to chew on.
        self.assertIn("Senior Software Engineer", scrape.job_content)
        # No synchronous JobPost — it materializes on the worker.
        self.assertIsNone(scrape.job_post_id)
        self.assertEqual(JobPost.objects.count(), 0)

        # Enqueued the from-text worker path, not a browser re-scrape.
        mock_async.assert_called_once()
        self.assertEqual(
            mock_async.call_args.args[0],
            "job_hunting.lib.tasks.parse_scrape_job",
        )
        self.assertEqual(mock_async.call_args.args[1], scrape.id)

    def test_extension_direct_description_only_worker_persists_jobpost(self):
        """Integration: run the enqueued worker leg and prove it persists
        a JobPost from the seeded job_content — the curated-miss capture
        actually becomes a post (title/company LLM-extracted from text).
        """
        from job_hunting.lib.tasks import parse_scrape_job

        link = "https://talent.toptal.com/portal/job/VjEtSm9iLTUwMTMzOQ"
        captured_innertext = (
            "Staff Backend Engineer\nToptal\nRemote\n"
            "Build the platform. " * 10
        )
        payload = {"description": captured_innertext}

        with patch("job_hunting.api.views.scrapes.async_task"):
            resp = self.client.post(
                "/api/v1/scrapes/",
                _post_body(
                    link,
                    source_mode="extension-direct",
                    captured_payload=payload,
                ),
                format="json",
            )
        self.assertEqual(resp.status_code, 201, resp.content)
        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])

        # Drive the worker leg with a mocked LLM extraction so the test
        # is deterministic and offline. analyze_with_ai is the single LLM
        # seam parse_scrape funnels through.
        with patch.object(
            JobPostExtractor, "analyze_with_ai",
            return_value=ParsedJobData(
                title="Staff Backend Engineer",
                company_name="Toptal",
                description="Build the platform. " * 10,
                location="Remote",
                link=link,
            ),
        ):
            parse_scrape_job(scrape.id, user_id=self.user.id)

        scrape.refresh_from_db()
        self.assertIsNotNone(scrape.job_post_id)
        jp = JobPost.objects.get(pk=scrape.job_post_id)
        self.assertEqual(jp.title, "Staff Backend Engineer")
        self.assertEqual(jp.company.name, "Toptal")

    def test_extension_direct_description_only_shell_is_never_persisted(self):
        """CC-122 invariant (Doug, 2026-07-19): a description-only capture
        is the INPUT to Tier 1, NEVER a persistable description-only
        JobPost. If the Tier 1 LLM cannot recover a real title/company
        from the captured text (it comes back with placeholders because
        the text was chrome/noise), process_evaluation must FAIL the
        scrape and create NO JobPost — "make lemonade" or fail, but never
        save a title/company-less shell.

        This locks the behavior against a future change that reads the
        (now-corrected) description-only relaxation as license to persist
        a bare description.
        """
        from job_hunting.lib.tasks import parse_scrape_job

        link = "https://www.linkedin.com/jobs/view/4437716572/"
        # Captured innerText that is UI chrome with no recoverable job
        # identity — the pathological curated-miss case.
        captured_innertext = "Apply now. Share. Save. Report this job. " * 20
        payload = {"description": captured_innertext}

        with patch("job_hunting.api.views.scrapes.async_task"):
            resp = self.client.post(
                "/api/v1/scrapes/",
                _post_body(
                    link,
                    source_mode="extension-direct",
                    captured_payload=payload,
                ),
                format="json",
            )
        self.assertEqual(resp.status_code, 201, resp.content)
        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])

        # Tier 1 LLM returns placeholder title/company — ParsedJobData
        # requires non-empty strings, so "no identity" surfaces as
        # placeholders, which process_evaluation is charged with rejecting.
        with patch.object(
            JobPostExtractor, "analyze_with_ai",
            return_value=ParsedJobData(
                title="Unknown",
                company_name="N/A",
                description="Apply now. Share. Save. " * 5,
                link=link,
            ),
        ):
            parse_scrape_job(scrape.id, user_id=self.user.id)

        scrape.refresh_from_db()
        # The scrape FAILS; no description-only shell is ever created.
        self.assertEqual(scrape.status, "failed")
        self.assertIsNone(scrape.job_post_id)
        self.assertEqual(JobPost.objects.count(), 0)
        # The placeholder-rejection reason survives to the operator surface.
        self.assertIn("placeholder", (scrape.failure_reason or "").lower())

    def test_browser_mode_keeps_409_dedupe_gate(self):
        """Regression guard — the dedupe-bypass is narrowly scoped to
        source_mode='extension-direct'. A vanilla browser-mode POST
        against a known link MUST still 409 so the chat-agent /
        bookmarklet flows don't mint redundant scrapes."""
        link = "https://example.com/jobs/browser-409"
        JobPost.objects.create(
            title="T",
            company=self.company,
            link=link,
            description="x" * 500,
            created_by=self.user,
        )

        resp = self.client.post(
            "/api/v1/scrapes/",
            _post_body(link),  # no source_mode, defaults to browser
            format="json",
        )
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertFalse(Scrape.objects.filter(url=link).exists())

    def test_browser_mode_create_still_produces_hold_scrape(self):
        """Regression — a browser-mode create (no source_mode, no payload)
        against an unknown URL still mints a `hold` scrape for the runner
        to claim, with no JobPost created synchronously. The Phase B
        synchronous consume is narrowly scoped to extension-direct and
        must not touch the browser path."""
        link = "https://example.com/jobs/browser-fresh"
        resp = self.client.post(
            "/api/v1/scrapes/", _post_body(link), format="json"
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(scrape.status, "hold")
        self.assertIsNone(scrape.job_post_id)
        self.assertEqual(scrape.source_mode, "browser")
        self.assertEqual(JobPost.objects.count(), 0)

    def test_extension_direct_canonical_link_collision_binds_existing_jp(self):
        """Canonical-link match still binds — the JP linkage on the
        scrape must be set even when the submitted URL differs from
        the JP's stored link by tracking params. Without this the
        runner can't find the existing JP and the fast-path would
        mint a duplicate."""
        # The JP stored link is the clean form; incoming carries
        # tracking junk that canonicalize_link() will strip.
        JobPost.objects.create(
            title="T",
            company=self.company,
            link="https://example.com/jobs/canonical-tied",
            canonical_link="https://example.com/jobs/canonical-tied",
            description="x" * 500,
            created_by=self.user,
        )
        dirty_url = (
            "https://example.com/jobs/canonical-tied"
            "?utm_source=ext&utm_campaign=fall"
        )

        resp = self.client.post(
            "/api/v1/scrapes/",
            _post_body(
                dirty_url,
                source_mode="extension-direct",
                captured_payload=_well_formed_payload(),
            ),
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        # Should be bound — canonical_link of submitted URL strips the
        # utm_* params and matches the existing JP's canonical_link.
        self.assertIsNotNone(scrape.job_post_id)


class ExtensionDirectMergeBiasTests(TestCase):
    """JobPostOverwriteDecision merge-bias rule — Phase A.

    When deciding which ``link`` to keep on a canonical-collision merge,
    prefer the row whose origin scrape carried
    ``source_mode='extension-direct'``. The user-rendered URL is more
    trustworthy than a background scrape's URL because the extension
    can only fire on a tab the user actually navigated to.

    Other fields stay on the existing empty-merge invariant — this rule
    only changes which link wins, not how title/company/description
    get merged.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="merge", password="p"
        )
        self.company = Company.objects.create(name="Acme Co")
        self.parsed_real = ParsedJobData(
            title="Real Job Title",
            company_name="Acme Co",
            description="Real description from the actual page. " * 20,
            location="Remote",
        )

    def test_helper_picks_incoming_when_incoming_is_extension_direct(self):
        # Unit test of the helper in isolation — incoming scrape is
        # extension-direct so its link wins regardless of the existing
        # JP's scrape history.
        existing_jp = JobPost.objects.create(
            title="T",
            company=self.company,
            link="https://example.com/jobs/old-browser-link",
            description="x" * 500,
            created_by=self.user,
        )
        Scrape.objects.create(
            url=existing_jp.link,
            job_post=existing_jp,
            source_mode="browser",
            created_by=self.user,
        )
        incoming_scrape = Scrape.objects.create(
            url="https://example.com/jobs/fresh-extension-link",
            source_mode="extension-direct",
            captured_payload=_well_formed_payload(),
            created_by=self.user,
        )

        chosen = prefer_extension_direct_link(
            existing_jp,
            incoming_scrape,
            "https://example.com/jobs/fresh-extension-link",
        )
        self.assertEqual(
            chosen, "https://example.com/jobs/fresh-extension-link"
        )

    def test_helper_keeps_existing_when_existing_is_extension_direct(self):
        # Inverse: existing JP carries an extension-direct scrape; a
        # later browser-mode scrape arrives. Keep the existing link —
        # the user already saw the extension-direct URL render.
        existing_jp = JobPost.objects.create(
            title="T",
            company=self.company,
            link="https://example.com/jobs/existing-extension-link",
            description="x" * 500,
            created_by=self.user,
        )
        Scrape.objects.create(
            url=existing_jp.link,
            job_post=existing_jp,
            source_mode="extension-direct",
            captured_payload=_well_formed_payload(),
            created_by=self.user,
        )
        incoming_scrape = Scrape.objects.create(
            url="https://example.com/jobs/later-browser-link",
            source_mode="browser",
            created_by=self.user,
        )

        chosen = prefer_extension_direct_link(
            existing_jp,
            incoming_scrape,
            "https://example.com/jobs/later-browser-link",
        )
        # The helper returns the existing JP's link so the caller's
        # `if job.link != chosen` short-circuits the overwrite — net
        # effect: link stays put.
        self.assertEqual(chosen, existing_jp.link)

    def test_helper_falls_through_when_no_extension_direct_signal(self):
        # Both sides are browser-mode (or have no scrape at all) →
        # helper returns the incoming link unchanged so the existing
        # trust-rank overwrite logic in _trust_aware_overwrite keeps
        # its historical behavior.
        existing_jp = JobPost.objects.create(
            title="T",
            company=self.company,
            link="https://example.com/jobs/existing-browser-link",
            description="x" * 500,
            created_by=self.user,
        )
        Scrape.objects.create(
            url=existing_jp.link,
            job_post=existing_jp,
            source_mode="browser",
            created_by=self.user,
        )
        incoming_scrape = Scrape.objects.create(
            url="https://example.com/jobs/another-browser-link",
            source_mode="browser",
            created_by=self.user,
        )

        chosen = prefer_extension_direct_link(
            existing_jp,
            incoming_scrape,
            "https://example.com/jobs/another-browser-link",
        )
        self.assertEqual(
            chosen, "https://example.com/jobs/another-browser-link"
        )

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_trust_overwrite_writes_extension_direct_link_to_jpod(
        self, mock_analyze
    ):
        """Integration: end-to-end through parse_scrape.

        Existing JP came from a low-trust source ("scrape") via a
        browser-mode scrape and points at the URL that scrape captured.
        Incoming extension scrape with source_mode='extension-direct'
        and a different (cleaner) URL hits the same canonical_link.
        Trust-aware overwrite fires (extension > scrape); link flips
        to the extension-direct URL and JPOD records the change.
        """
        # Both URLs canonicalize to the same form (utm params stripped).
        old_link = (
            "https://example.com/jobs/role-99?utm_source=indeed"
        )
        new_link = "https://example.com/jobs/role-99"
        mock_analyze.return_value = ParsedJobData(
            title="Real Job Title",
            company_name="Acme Co",
            description="Real description. " * 20,
            location="Remote",
        )

        existing_jp = JobPost.objects.create(
            title="Stale Title",
            company=self.company,
            link=old_link,
            description="stale description " * 30,
            created_by=self.user,
            source="scrape",  # trust 70
            complete=True,
        )
        # Make sure existing JP has a browser-mode scrape on file so
        # the helper sees the asymmetric "extension-direct only on
        # incoming" signal.
        Scrape.objects.create(
            url=old_link,
            job_post=existing_jp,
            source_mode="browser",
            source="scrape",
            created_by=self.user,
        )

        incoming_scrape = Scrape.objects.create(
            url=new_link,
            status="completed",
            job_content="real page text " * 50,
            source="extension",  # trust 100 — outranks "scrape"
            source_mode="extension-direct",
            captured_payload=_well_formed_payload(),
            created_by=self.user,
        )

        parse_scrape(incoming_scrape.id, user_id=self.user.id, sync=True)

        existing_jp.refresh_from_db()
        # Link flipped to the extension-direct URL.
        self.assertEqual(existing_jp.link, new_link)
        # Audit row records the flip.
        decision = JobPostOverwriteDecision.objects.filter(
            job_post=existing_jp
        ).first()
        self.assertIsNotNone(decision)
        self.assertIn("link", decision.changed_fields)
        self.assertEqual(decision.changed_fields["link"]["before"], old_link)
        self.assertEqual(decision.changed_fields["link"]["after"], new_link)

    @patch.object(JobPostExtractor, "analyze_with_ai")
    def test_existing_extension_direct_keeps_link_under_browser_incoming(
        self, mock_analyze
    ):
        """Inverse integration case: existing JP's authoritative URL
        came from an extension-direct capture. A later browser-mode
        scrape from a HIGHER-trust source must NOT overwrite the link —
        the user-attested URL is the canonical one.

        We pick existing source="email" (trust 20) and incoming
        source="extension" (trust 100) so the trust check still fires
        (otherwise the merge-empty path runs and link doesn't move
        either way). The link must stay on the original extension-
        direct URL despite the trust differential.
        """
        kept_link = "https://example.com/jobs/extension-kept"
        new_link = "https://example.com/jobs/browser-incoming"
        mock_analyze.return_value = ParsedJobData(
            title="Real Job Title",
            company_name="Acme Co",
            description="Real description. " * 20,
        )

        existing_jp = JobPost.objects.create(
            title="Stale Title",
            company=self.company,
            link=kept_link,
            canonical_link=kept_link,
            description="stale description " * 30,
            created_by=self.user,
            source="email",  # trust 20
            complete=True,
        )
        # The extension-direct scrape that established the kept_link.
        Scrape.objects.create(
            url=kept_link,
            job_post=existing_jp,
            source_mode="extension-direct",
            source="extension",
            captured_payload=_well_formed_payload(),
            created_by=self.user,
        )

        # Force canonical_link collision: existing has canonical=kept_link,
        # incoming's url canonicalizes differently but we point the lookup
        # via link= match on a freshly-saved JP. Simulating "same canonical
        # but different raw link" cleanly requires bypassing canonicalization
        # — easier here: keep the same effective canonical via a URL
        # that we manually align by setting the JobPost.canonical_link to
        # the incoming canonical post-save. The integration test for the
        # forward case (above) covers the canonicalize-from-utm path; this
        # test only needs to prove the link-decision direction.
        incoming_scrape = Scrape.objects.create(
            url=new_link,
            status="completed",
            job_content="real page text " * 50,
            source="extension",  # outranks email
            source_mode="browser",
            created_by=self.user,
        )
        # Force existing JP to share the same canonical_link the incoming
        # scrape will use, so find_duplicate's stage-1 hits.
        existing_jp.canonical_link = new_link
        existing_jp.save(update_fields=["canonical_link"])

        parse_scrape(incoming_scrape.id, user_id=self.user.id, sync=True)

        existing_jp.refresh_from_db()
        # Link is NOT flipped to the browser-mode URL — extension-direct
        # on existing wins despite incoming being higher trust.
        self.assertEqual(existing_jp.link, kept_link)
