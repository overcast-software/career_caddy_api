from django.conf import settings
from django.db import models
from .base import GetMixin
from .nanoid_pk import NanoIDModel
from urllib.parse import urlparse


class Scrape(GetMixin, NanoIDModel):
    # ``id`` is the 10-char NanoID string PK from NanoIDModel (CC-77 #79
    # true PK swap). FKs referencing scrape(id): scrape_status.scrape_id
    # (CASCADE, NOT NULL), the self-FK scrape.source_scrape_id (SET_NULL),
    # and triggering_scrape_id on job_post_overwrite_decision /
    # job_post_description_decision (both SET_NULL, nullable).
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="created_by",
        related_name="created_scrapes",
    )
    url = models.CharField(max_length=2000, null=True, blank=True)
    # The original URL the caller submitted, before tracker resolution.
    # Set when ``url`` was rewritten from a tracker (SendGrid click,
    # LinkedIn /comm/, etc.) to its destination at ingest time.
    source_link = models.CharField(max_length=2000, null=True, blank=True)
    company = models.ForeignKey(
        "Company",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scrapes",
    )
    job_post = models.ForeignKey(
        "JobPost",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scrapes",
    )
    # Provenance: how this scrape was created. Copied onto the JobPost
    # when parse_scrape creates one so downstream analytics can attribute
    # posts to their origin (email pipeline vs user paste vs user scrape).
    source = models.CharField(max_length=32, default="manual")
    css_selectors = models.TextField(null=True, blank=True)
    job_content = models.TextField(null=True, blank=True)
    external_link = models.CharField(max_length=2000, null=True, blank=True)
    parse_method = models.CharField(max_length=100, null=True, blank=True)
    scraped_at = models.DateTimeField(null=True, blank=True)
    # Creation timestamp — the FIFO key for the claim-next queue. Added
    # in CC-77: the PK is now a (random) NanoID, so the integer-autoinc
    # id can no longer stand in as the arrival-order key. Nullable so the
    # additive migration needs no backfill; pre-existing rows sort first
    # (nulls_first) as the oldest holds.
    created_at = models.DateTimeField(auto_now_add=True, null=True)
    status = models.CharField(max_length=50, null=True, blank=True)
    source_scrape = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="source_scrape_id",
        related_name="child_scrapes",
    )
    html = models.TextField(null=True, blank=True)
    # Mirror of JobPost.apply_url/status — each scrape carries the
    # resolver outcome it produced so multi-scrape histories stay truthful.
    apply_url = models.CharField(max_length=2000, null=True, blank=True)
    apply_url_status = models.CharField(max_length=16, default="unknown")
    # The referring URL captured at submit time by the browser extension
    # (ccsender) — the page the user came from. Indexed so
    # compute_duplicate_candidates can join on it to surface cross-platform
    # dedup relationships bidirectionally on the candidate panel.
    referrer_url = models.CharField(
        max_length=2000, null=True, blank=True, db_index=True
    )
    # Per-field structured values the browser extension extracted client-side
    # using ScrapeProfile.css_selectors.job_data. Shape mirrors the selector
    # dict keys (title, company_name, location, salary, description, …) — null
    # per field on a selector miss. Read by JobPostExtractor._try_prefill_
    # extraction as a $0 fast-path that bypasses the LLM when title +
    # company_name are present.
    extension_prefill = models.JSONField(null=True, blank=True)
    # Phase 3 learning loop: when the resolver ends in unknown/failed,
    # the ai-side heuristic scan stores candidate "Apply" elements
    # here for later aggregation + promotion into
    # ScrapeProfile.apply_resolver_config. Shape:
    #   [{"selector": ".btn.apply", "href": "https://...", "text": "Apply",
    #     "tag": "a", "score": 0.8, "reason": "href contains 'apply'"}]
    # Capture-only — promotion is a separate manual/automated step.
    apply_candidates = models.JSONField(null=True, blank=True)
    # Phase A of the dedupe redesign — top-3 trigram-similar Companies
    # to whatever name the extractor saw, stashed at extraction time
    # for staff review. Written ONLY when no exact ``CompanyAlias`` hit
    # exists (i.e. the extractor minted a new Company). Frontend
    # surfaces this as a "Suggested companies" callout on Scrape show
    # so a curator can hit Merge-into and consolidate. Shape:
    #   [{"company_id": int, "name": str, "similarity": float}, ...]
    # Never auto-applied — Doug's option (b) gate: fuzzy similarity
    # never reaches Company.find_by_alias; only exact ``name_slug``
    # match auto-attaches. See plan
    # ``go-over-this-plan-staged-sutherland.md`` Phase A and api
    # notes.org ``Architecture/Dedupe pipeline contract``.
    company_suggestions = models.JSONField(null=True, blank=True)
    # Closed-state detection result from the scrape-graph
    # DetectClosedState node. Written by the agents-side graph PATCH on
    # PersistScrape; read by JobPostExtractor as the priority-1 channel
    # for posting_status flips (priority over the extractor's own raw-
    # source phrase scan and LLM-emitted closed_evidence). Three-tier
    # detection inside the node — CSS selector hit / phrase hit / Haiku-
    # validated quote — collapses into this single column. Method +
    # which-list-fired live in the graph trace for diagnostics; the
    # extractor only reads the post-facto verdict.
    detected_posting_status = models.CharField(
        max_length=16, null=True, blank=True
    )
    detected_closed_evidence = models.TextField(null=True, blank=True)
    # When True, the scrape-graph runs Navigate → ResolveFinalUrl →
    # CheckLinkDedup → (page-load) → ResolveApplyUrl → End and SKIPS
    # the StartExtract → Tier* → PersistJobPost → ReviewCompleteness →
    # UpdateProfile chain. Used by the staff "Resolve & dedupe" action
    # on jp.edit: kicks off a real browser fetch to settle JS / meta-
    # refresh redirects (e.g. ZipRecruiter /km/<token>) and capture the
    # apply destination, but does not LLM-extract or overwrite the
    # originating JobPost. CheckLinkDedup → DuplicateShortCircuit still
    # fires on canonical-link match, which is the load-bearing dedupe
    # mechanic for tracker-URL stubs.
    skip_extract = models.BooleanField(default=False)
    # Phase 1 of Plans/Scrape runner — harden hold-poller.
    # claimed_at + claimed_by support atomic claim by N coexisting
    # runners (omarchy, pibu, …) via SELECT FOR UPDATE SKIP LOCKED on
    # POST /scrapes/claim-next/. Bumped on each status update during the
    # pipeline so a live runner's claim doesn't expire mid-scrape; reset
    # to NULL by the lease-sweep when claimed_at < NOW() - 15min and
    # status is non-terminal (covers runner-crash recovery).
    claimed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    claimed_by = models.CharField(max_length=100, null=True, blank=True)
    # Attended-scrape routing. Partitions the `status='hold'` claim queue
    # so an interactive ("attended") runner — a headed browser a human is
    # actively driving to solve logins/captchas — picks up ONLY the rows
    # flagged for it, and the default unattended runners NEVER do.
    # POST /scrapes/claim-next/ filters on this column (hence db_index):
    #   attended absent/False -> claims oldest hold AND attended=False
    #   attended=True          -> claims oldest hold AND attended=True
    # OPERATIONAL CONSEQUENCE: an attended-marked scrape is processed ONLY
    # when an attended runner is running. If none is, it sits in `hold`
    # indefinitely — default runners skip it by design. v1 accepts this;
    # a staleness fallback (auto-demote stale attended holds) is a possible
    # follow-up but is intentionally NOT built here.
    attended = models.BooleanField(default=False, db_index=True)
    # Phase A — Extension direct-POST plan. How this scrape was *captured*,
    # as opposed to `source` which records the JobPost write provenance.
    # `browser`        — historical default. Camoufox/Playwright fetches
    #                    the page server-side via the scrape graph
    #                    (Navigate → DetectObstacle → … → PersistJobPost).
    # `extension-direct` — extension content-script already extracted
    #                    title + company + description from the user-
    #                    rendered DOM. Phase B's StartScrape gate will
    #                    branch the graph past every browser-tier node
    #                    and feed the captured payload straight into
    #                    PersistJobPost. Until Phase B lands, the field
    #                    is metadata only.
    # Indexed so the future Phase D operator panel can count fast-path
    # scrapes per day cheaply.
    SOURCE_MODE_CHOICES = (
        ("browser", "browser"),
        ("extension-direct", "extension-direct"),
    )
    source_mode = models.CharField(
        max_length=32,
        default="browser",
        choices=SOURCE_MODE_CHOICES,
        db_index=True,
    )
    # Phase A — Extension direct-POST plan. Extension-side capture payload
    # for `source_mode='extension-direct'` rows. Shape (the v1 contract —
    # Doug's "trust presence, iterate" rule, no validator threshold):
    #   {
    #     "title": "<non-empty str>",          # required
    #     "company": "<non-empty str>",        # required
    #     "description": "<non-empty str>",    # required
    #     "apply_url": "<str>",                # optional
    #     "location": "<str>",                 # optional
    #     "extraction_hints": {<dict>},        # optional (per-host hints)
    #   }
    # NULL for `source_mode='browser'` rows (validated at write time —
    # the serializer rejects a browser-mode write that carries a payload
    # so a paste path can't echo a stale field). Phase B's SkipBrowserTier
    # node reads this and feeds the fields into PersistJobPost.
    captured_payload = models.JSONField(null=True, blank=True, default=None)
    # Human-readable summary of why this scrape didn't produce a
    # JobPost. Written at every ``status="failed"`` write site (the
    # placeholder rejections in ``JobPostExtractor.process_evaluation``,
    # the exception paths in ``parse_scrape._run``, and any caller of
    # ``_log_scrape_status(... status_label="failed", failure_reason=…)``)
    # so the operator has a diagnostic surface — extension popup,
    # scrapes.show, dedupe report all read this field. Truncated to
    # 2000 chars at write time. NULL for non-failed rows; existing
    # pre-feature failed rows stay NULL (no backfill).
    failure_reason = models.TextField(null=True, blank=True, max_length=2000)

    class Meta:
        db_table = "scrape"

    @property
    def host(self):
        if self.url:
            return urlparse(self.url).netloc
        return None

    @property
    def latest_status_note(self):
        """Note from the most recent ScrapeStatus entry, or empty string.

        Exposed on the serializer so clients can branch UI behavior on
        completion outcomes (e.g. 'duplicate: existing JobPost #N') without
        having to sideload the full scrape_statuses collection.
        """
        latest = self.scrape_statuses.order_by("-id").first()
        if latest is None:
            return ""
        return latest.note or ""
