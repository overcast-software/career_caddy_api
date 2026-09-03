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


def _hinted_payload(
    *,
    canonical_link_hint=None,
    referrer_url=None,
    **curated_kwargs,
):
    """``_curated_payload`` plus the two sibling extraction_hints (BACK-131).

    ``send-gate.ts:126-127`` nests ``canonical_link_hint`` and
    ``referrer_url`` alongside ``structured_prefill`` under
    ``captured_payload.extraction_hints`` — a different shape from
    ``/scrapes/from-text/``, which takes all three top-level. This helper
    builds the nested (extension-direct) shape so tests exercise the real
    wire format rather than a convenient approximation.
    """
    payload = _curated_payload(**curated_kwargs)
    if canonical_link_hint is not None:
        payload["extraction_hints"]["canonical_link_hint"] = canonical_link_hint
    if referrer_url is not None:
        payload["extraction_hints"]["referrer_url"] = referrer_url
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
                    "job_hunting.api.views.scrapes.enqueue"
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
                # Enqueued the SAME worker path from-text uses (CC-207b kind).
                self.assertEqual(mock_async.call_args.args[0], "parse_scrape")

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
        """Defense-in-depth for the description-source rule: when the payload
        carries NO structured_prefill.description, the raw top-level
        description (full-page innerText) must not become the JobPost
        description.

        The *outcome* changed here, the rule did not. This test used to
        assert the post was created with title/company and an EMPTY
        description — page chrome kept out, but a shell saved, which scoring
        then rejects with "Job post has no description to score against".
        CC-122's rule is "take the data and make lemonade, or fail — never
        save a shell", so a Tier-0 hit now requires all three fields and a
        missing description escalates to Tier 1 instead of persisting.

        The chrome-exclusion half is unchanged and still pinned: no post
        exists yet, and the raw text goes to job_content for the LLM rather
        than to any description field.
        """
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

        # Escalated, not completed — no shell persisted.
        self.assertEqual(scrape.status, "pending")
        self.assertIsNone(scrape.job_post_id)
        self.assertFalse(
            JobPost.objects.filter(link=link).exists(),
            "a description-less Tier-0 hit must not mint a JobPost",
        )

        # The raw innerText is staged for the LLM, not used as a description.
        self.assertIn("page has loaded", scrape.job_content or "")

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

        with patch("job_hunting.api.views.scrapes.enqueue") as mock_async:
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

        # Enqueued the from-text worker path (CC-207b kind), not a browser
        # re-scrape. scrape_id rides the payload as a kwarg.
        mock_async.assert_called_once()
        self.assertEqual(mock_async.call_args.args[0], "parse_scrape")
        self.assertEqual(mock_async.call_args.kwargs["scrape_id"], scrape.id)

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

        with patch("job_hunting.api.views.scrapes.enqueue"):
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

        with patch("job_hunting.api.views.scrapes.enqueue"):
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
        mint a duplicate.

        Uses ``_curated_payload`` rather than ``_well_formed_payload``: this
        test needs a Tier-0 HIT to reach the binding code, and a Tier-0 hit
        resolves its description from ``extraction_hints.structured_prefill``
        only, which the minimal helper does not carry. That is a fixture
        detail — the invariant under test is the canonical-link binding, not
        where the description comes from.
        """
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
                captured_payload=_curated_payload(
                    title="Senior Widget Engineer",
                    company_name="Acme Co",
                    description="Build widgets at scale. " * 5,
                ),
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
        """TEST CHANGED DELIBERATELY (2026-09, CC-257 Stage 5c.1) — not
        adjusted to pass. This test used to assert that an
        extension-direct push at a CLEANER url (same canonical, different
        string) found the utm-dirty existing row via the canonical_link
        OR-leg and trust-overwrote it in place, flipping its link and
        writing a JPOD audit row. That reachability is exactly what
        Stage 5c.1 removed: an overwrite may only fire on a byte-equal
        link or the scrape's explicit job_post_id FK, because the same
        guess that healed a utm-dirty twin also destroyed distinct jobs
        whose URLs merely canonicalized alike.

        New contract, pinned here: the variant-URL push FORKS a second
        row (the ruling — duplicates are acceptable), the existing row is
        untouched, and no overwrite audit row is written. The self-heal
        concern moves to the read side: the extension's filter[link]
        popup lookup still four-leg matches, so the user SEES the
        existing post and the human verbs reconcile the pair. When the
        extension push is FK-bound to the post (scrape.job_post_id), the
        overwrite still fires — that path is covered by
        test_fk_linked_scrape_still_upgrades_across_url_variants in
        test_job_post_extractor.py.
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

        # The existing row is byte-for-byte untouched — no overwrite
        # through a canonical guess, no link flip, no audit row.
        existing_jp.refresh_from_db()
        self.assertEqual(existing_jp.link, old_link)
        self.assertEqual(existing_jp.title, "Stale Title")
        self.assertEqual(existing_jp.source, "scrape")
        self.assertIsNone(
            JobPostOverwriteDecision.objects.filter(
                job_post=existing_jp
            ).first()
        )
        # The push minted its own row at the extension-direct URL.
        forked = JobPost.objects.filter(link=new_link).first()
        self.assertIsNotNone(forked)
        self.assertNotEqual(forked.id, existing_jp.id)
        self.assertEqual(forked.title, "Real Job Title")
        incoming_scrape.refresh_from_db()
        self.assertEqual(incoming_scrape.job_post_id, forked.id)

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


class ExtensionDirectHintPlumbingTests(TestCase):
    """BACK-131 — the three hints the extension sends on every Send.

    ``send-gate.ts`` puts ``apply_url`` at the top level of
    captured_payload and ``canonical_link_hint`` / ``referrer_url`` under
    ``captured_payload.extraction_hints``. Before this change the consume
    path read only ``extraction_hints.structured_prefill``, so the sibling
    three were discarded with no error and no log — which is why
    ``apply_url`` was populated on ~3 of 100 recently-created prod posts.

    CC-176 made extension-direct the only route for any page with text, so
    ``/scrapes/from-text/`` — the route that always honoured these — is
    unreachable in practice. These tests pin the fast path to the same
    behaviour on BOTH tiers: the synchronous Tier-0 hit stamps inline, the
    Tier-0 miss forwards to the worker. Fixing only one branch would move
    the bug rather than close it.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="dough", password="p", is_staff=True
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_tier0_hit_stamps_canonicalized_apply_url(self):
        """AC1 — a valid apply_url lands on the JobPost, canonicalized,
        with apply_url_status='resolved'.

        Canonicalization is the point, not a detail: tracking params in an
        apply destination break the exact-equality legs of the
        ``filter[link]`` lookup AND the apply_url leg of find_duplicate
        (``find_apply_url_matches``), which is the strongest signal for
        recognising the same role reached via two different boards.
        """
        link = "https://example.com/jobs/hint-apply-url"
        resp = self.client.post(
            "/api/v1/scrapes/",
            _post_body(
                link,
                source_mode="extension-direct",
                captured_payload=_curated_payload(
                    title="Platform Engineer",
                    company_name="StampCo",
                    description="Real curated description. " * 20,
                    apply_url=(
                        "https://ats.example.com/apply/77?utm_source=linkedin"
                        "&utm_campaign=q3"
                    ),
                ),
            ),
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)

        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(scrape.status, "completed")
        jp = JobPost.objects.get(pk=scrape.job_post_id)
        # Tracking params stripped by canonicalize_apply_url — the same
        # helper parse_scrape_job uses, not a second implementation.
        self.assertEqual(jp.apply_url, "https://ats.example.com/apply/77")
        self.assertEqual(jp.apply_url_status, "resolved")

    def test_tier0_hit_without_apply_url_leaves_column_untouched(self):
        """The stamp is opt-in: a payload with no apply_url must not write
        an empty value or flip apply_url_status off its default. Guards a
        regression where the stamp fires unconditionally and blanks a
        value an earlier resolver had already found."""
        link = "https://example.com/jobs/hint-no-apply-url"
        resp = self.client.post(
            "/api/v1/scrapes/",
            _post_body(
                link,
                source_mode="extension-direct",
                captured_payload=_curated_payload(
                    title="Platform Engineer",
                    company_name="StampCo",
                    description="Real curated description. " * 20,
                ),
            ),
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)

        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        jp = JobPost.objects.get(pk=scrape.job_post_id)
        self.assertFalse(jp.apply_url)
        self.assertEqual(jp.apply_url_status, "unknown")

    def test_invalid_apply_url_is_dropped_not_4xxd(self):
        """AC3 — a bad hint must never block ingestion.

        ``from_text`` drops malformed/policy-blocked hints silently, and
        the two paths must not disagree: a private-network apply_url is a
        dropped field, not a rejected Send. The post still persists in
        full; only the hint is discarded.
        """
        link = "https://example.com/jobs/hint-bad-apply-url"
        resp = self.client.post(
            "/api/v1/scrapes/",
            _post_body(
                link,
                source_mode="extension-direct",
                captured_payload=_curated_payload(
                    title="Platform Engineer",
                    company_name="StampCo",
                    description="Real curated description. " * 20,
                    apply_url="http://192.168.1.1/admin",
                ),
            ),
            format="json",
        )
        # The Send succeeds — that is the whole point of AC3.
        self.assertEqual(resp.status_code, 201, resp.content)

        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(scrape.status, "completed")
        jp = JobPost.objects.get(pk=scrape.job_post_id)
        self.assertEqual(jp.title, "Platform Engineer")
        self.assertFalse(jp.apply_url)
        self.assertEqual(jp.apply_url_status, "unknown")

    def test_malformed_apply_url_is_dropped_not_4xxd(self):
        """Same contract for a non-http string — the extension's decoder
        can emit garbage when a site changes its apply-button markup, and
        that must degrade to "no hint", never to a failed Send."""
        link = "https://example.com/jobs/hint-garbage-apply-url"
        resp = self.client.post(
            "/api/v1/scrapes/",
            _post_body(
                link,
                source_mode="extension-direct",
                captured_payload=_curated_payload(
                    title="Platform Engineer",
                    company_name="StampCo",
                    description="Real curated description. " * 20,
                    apply_url="javascript:void(0)",
                ),
            ),
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)

        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        jp = JobPost.objects.get(pk=scrape.job_post_id)
        self.assertFalse(jp.apply_url)

    def test_canonical_link_hint_overrides_submitted_url(self):
        """AC2 — the page's own canonical (LinkedIn og:url) wins over the
        tracker-laden location.href the extension submits.

        Same override ``from_text`` applies at :1129-1130. It reaches both
        the persisted Scrape.url and the JobPost, because the consume path
        hands ``scrape.url`` to _parsed_job_data_from_payload as
        ParsedJobData.link — which is what process_evaluation's dedupe walk
        resolves against.
        """
        submitted = (
            "https://www.linkedin.com/jobs/view/4437716572/"
            "?refId=abc123&trackingId=xyz789"
        )
        canonical = "https://www.linkedin.com/jobs/view/4437716572/"
        resp = self.client.post(
            "/api/v1/scrapes/",
            _post_body(
                submitted,
                source_mode="extension-direct",
                captured_payload=_hinted_payload(
                    title="Staff Engineer",
                    company_name="HintCo",
                    description="Real curated description. " * 20,
                    canonical_link_hint=canonical,
                ),
            ),
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)

        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(scrape.url, canonical)
        jp = JobPost.objects.get(pk=scrape.job_post_id)
        self.assertEqual(jp.link, canonical)

    def test_invalid_canonical_link_hint_leaves_submitted_url(self):
        """A dropped canonical hint falls back to the submitted URL, not
        to None — otherwise a bad hint would strand the scrape with no
        link at all."""
        submitted = "https://example.com/jobs/hint-bad-canonical"
        resp = self.client.post(
            "/api/v1/scrapes/",
            _post_body(
                submitted,
                source_mode="extension-direct",
                captured_payload=_hinted_payload(
                    title="Staff Engineer",
                    company_name="HintCo",
                    description="Real curated description. " * 20,
                    canonical_link_hint="http://10.0.0.5/internal",
                ),
            ),
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)

        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(scrape.url, submitted)

    def test_referrer_url_persisted_on_scrape(self):
        """AC2 — referrer_url lands on Scrape.referrer_url.

        That column IS the storage for this signal (there is no symmetric
        field on JobPost), and it is db_index'd precisely so
        compute_duplicate_candidates can join it against a candidate's
        link/canonical_link and emit the high-confidence ``referrer_hint``
        candidate. Persisting it is what makes the referrer->ATS pairing
        live on this path.
        """
        resp = self.client.post(
            "/api/v1/scrapes/",
            _post_body(
                "https://ats.example.com/jobs/hint-referrer",
                source_mode="extension-direct",
                captured_payload=_hinted_payload(
                    title="Staff Engineer",
                    company_name="HintCo",
                    description="Real curated description. " * 20,
                    referrer_url="https://www.linkedin.com/jobs/view/999/",
                ),
            ),
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)

        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(
            scrape.referrer_url, "https://www.linkedin.com/jobs/view/999/"
        )

    def test_invalid_referrer_url_dropped_not_4xxd(self):
        """A private-network referrer is dropped; the Send still lands."""
        resp = self.client.post(
            "/api/v1/scrapes/",
            _post_body(
                "https://ats.example.com/jobs/hint-bad-referrer",
                source_mode="extension-direct",
                captured_payload=_hinted_payload(
                    title="Staff Engineer",
                    company_name="HintCo",
                    description="Real curated description. " * 20,
                    referrer_url="http://192.168.1.1/admin",
                ),
            ),
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)

        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertIsNone(scrape.referrer_url)

    def test_referrer_url_absent_on_browser_mode_create(self):
        """Browser-mode writes carry no captured_payload, so the hint read
        is skipped entirely and referrer_url stays null — the pre-BACK-131
        behaviour for every non-extension-direct caller."""
        resp = self.client.post(
            "/api/v1/scrapes/",
            _post_body("https://example.com/jobs/browser-mode-no-hints"),
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)

        scrape = Scrape.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(scrape.status, "hold")
        self.assertIsNone(scrape.referrer_url)

    def test_tier1_escalation_forwards_apply_url_to_worker(self):
        """AC4 — the Tier-0 MISS branch carries apply_url too.

        The escalation enqueues parse_scrape_job, which performs the
        identical canonicalize-and-stamp. Omitting the kwarg would just
        relocate the bug onto the auth-walled pages (LinkedIn/Toptal) that
        are the whole reason the Tier-1 path exists — and those are
        exactly the pages whose apply_url is most worth having, since the
        posting itself is unfetchable.
        """
        link = "https://www.linkedin.com/jobs/view/4437716572/"
        captured_innertext = (
            "Senior Software Engineer at BigCorp. We are hiring engineers "
            "to build distributed systems. Apply now. " * 10
        )
        payload = {
            "description": captured_innertext,
            "apply_url": "https://ats.example.com/apply/88?utm_source=li",
            "extraction_hints": {
                "referrer_url": "https://www.linkedin.com/jobs/search/",
            },
        }

        with patch("job_hunting.api.views.scrapes.enqueue") as mock_enqueue:
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
        self.assertEqual(scrape.status, "pending")
        # The referrer leg is handled at create time, so it is persisted
        # regardless of which tier ends up resolving the capture.
        self.assertEqual(
            scrape.referrer_url, "https://www.linkedin.com/jobs/search/"
        )

        mock_enqueue.assert_called_once()
        _args, kwargs = mock_enqueue.call_args
        self.assertEqual(kwargs["scrape_id"], scrape.id)
        # Validated but NOT canonicalized here — parse_scrape_job owns the
        # canonicalization, so the worker receives the same shape of value
        # from_text hands it.
        self.assertEqual(
            kwargs["apply_url"],
            "https://ats.example.com/apply/88?utm_source=li",
        )
        # auto_score deliberately not passed: starting a score on this path
        # is a separate decision with a documented double-scoring hazard
        # (seven existing score starters, enqueue has no dedup key).
        self.assertNotIn("auto_score", kwargs)

    def test_tier1_escalation_drops_invalid_apply_url(self):
        """The drop-don't-4xx rule holds on the escalation branch too: an
        unusable hint reaches the worker as None rather than failing the
        Send or handing the worker a value it would stamp verbatim."""
        link = "https://www.linkedin.com/jobs/view/4437716573/"
        payload = {
            "description": "Senior Software Engineer at BigCorp. " * 20,
            "apply_url": "http://192.168.1.1/admin",
        }

        with patch("job_hunting.api.views.scrapes.enqueue") as mock_enqueue:
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

        mock_enqueue.assert_called_once()
        _args, kwargs = mock_enqueue.call_args
        self.assertIsNone(kwargs["apply_url"])


class ExtensionDirectShellAndJobContentTests(TestCase):
    """Two invariants on the Tier-0 fast path.

    1. A Tier-0 HIT requires a description. Without one the result is a
       SHELL, and CC-122's rule is explicit: "take the data and make
       lemonade, or fail — never save a shell." That rule was written about
       a description with no title/company; this is the same shell from the
       other side, and Tier 1 is the same remedy.

    2. `job_content` is seeded on the HIT path, not only on the escalation.
       Closed-state detection and the closed_evidence substring check both
       read it and both are guarded on it being non-empty. The paste/
       extension description fallback reads it too, but invariant 1 is what
       keeps that fallback from firing here — see
       ``test_tier0_hit_description_still_comes_from_structured_prefill``.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="shelltest", password="p", is_staff=True
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _create(self, payload, url="https://boards.example.com/jobs/9001"):
        return self.client.post(
            "/api/v1/scrapes/",
            _post_body(url, source_mode="extension-direct", captured_payload=payload),
            format="json",
        )

    def test_missing_description_escalates_instead_of_saving_a_shell(self):
        """THE LINKEDIN CASE.

        Migration 0093 seeds linkedin.com with exactly two selectors —
        `title: h1` and `company_name: a[href*='/company/']` — and
        deliberately no description. So structured_prefill carries title and
        company and nothing else.

        Before this fix that was a Tier-0 HIT: ParsedJobData.description is
        Optional, so `description=None` validated happily and the post was
        saved without one. Scoring then failed with "Job post has no
        description to score against".
        """
        resp = self._create(
            _curated_payload(
                title="Senior Cloud Security Engineer",
                company_name="Rescale",
                description=None,
                raw_description="Senior Cloud Security Engineer at Rescale. " * 20,
            )
        )
        self.assertIn(resp.status_code, (200, 201, 202))
        scrape = Scrape.objects.order_by("-created_at").first()

        # Escalated, not completed — Tier 1 will recover the description
        # from the innerText.
        self.assertEqual(scrape.status, "pending")
        self.assertIsNone(scrape.job_post_id)

        # And the escalation seeded the text the LLM needs.
        self.assertTrue((scrape.job_content or "").strip())

    def test_tier0_hit_seeds_job_content(self):
        """A complete Tier-0 hit still persists the raw innerText, because
        closed-state detection and the description fallback both read it and
        both are guarded on it being non-empty."""
        raw = "No longer accepting applications. " + ("body text " * 40)
        resp = self._create(
            _curated_payload(
                title="Backend Engineer",
                company_name="Acme",
                description="We are looking for a backend engineer. " * 10,
                raw_description=raw,
            )
        )
        self.assertIn(resp.status_code, (200, 201, 202))
        scrape = Scrape.objects.order_by("-created_at").first()

        self.assertEqual(scrape.status, "completed")
        self.assertIsNotNone(scrape.job_post_id)
        self.assertEqual(scrape.job_content, raw.strip())

    def test_tier0_hit_description_still_comes_from_structured_prefill(self):
        """Seeding job_content must NOT change where the description comes
        from. The raw innerText is nav/footer noise; the clean per-selector
        value is what the post gets."""
        clean = "Clean description from the selector. " * 8
        resp = self._create(
            _curated_payload(
                title="Data Engineer",
                company_name="Globex",
                description=clean,
                raw_description="Skip to main content. Cookie banner. " * 30,
            )
        )
        self.assertIn(resp.status_code, (200, 201, 202))
        scrape = Scrape.objects.order_by("-created_at").first()
        self.assertIsNotNone(scrape.job_post_id)
        self.assertEqual(scrape.job_post.description.strip(), clean.strip())
