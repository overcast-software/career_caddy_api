from django.conf import settings
from django.db import models
from django.utils import timezone

from .base import GetMixin
from .job_post_dedupe import canonicalize_link, fingerprint, strip_url_trailing_junk


# AS2 (ActivityStreams 2.0) Public collection URI. Posts whose `audience`
# list contains this string are public — visible to anyone, federable
# without restriction. Phase 3.5 prep for Phase 4 ActivityPub readiness:
# federation visibility lives on each JobPost via the AS2 `audience`
# primitive instead of being inferred from content shape.
AS2_PUBLIC = "https://www.w3.org/ns/activitystreams#Public"


def _default_audience_public():
    """Callable default for JobPost.audience.

    JSONField with a mutable default (list/dict) MUST receive a callable,
    not a literal — Django would otherwise share one list instance across
    every freshly-instantiated row. Returning a fresh list per call keeps
    per-row mutations isolated.
    """
    return [AS2_PUBLIC]


def _default_source_instance():
    """Callable default for JobPost.source_instance.

    Resolved at row-creation time (NOT module-import time) so the value
    reflects the running process's CAREER_CADDY_INSTANCE — important for
    tests that override settings and for the rare same-binary multi-
    instance dev setup. Returning a string per call (not a settings
    lookup baked into the migration) keeps fixtures portable.
    """
    return settings.CAREER_CADDY_INSTANCE


class JobPost(GetMixin, models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="created_by",
        related_name="created_job_posts",
    )
    description = models.TextField(null=True, blank=True)
    title = models.CharField(max_length=255, null=True, blank=True)
    company = models.ForeignKey(
        "Company",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="job_posts",
    )
    posted_date = models.DateField(null=True, blank=True)
    extraction_date = models.DateField(null=True, blank=True)
    link = models.CharField(max_length=1000, null=True, blank=True, unique=True)
    salary_min = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    salary_max = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    location = models.CharField(max_length=255, null=True, blank=True)
    remote = models.BooleanField(null=True, blank=True)
    # Provenance: where the post entered the system. Values come from
    # the calling code path (manual, paste, scrape, email, chat, ...);
    # free-form CharField rather than an enum so future sources can
    # appear without migrations. Defaults to 'manual' so historical
    # rows backfill safely.
    source = models.CharField(max_length=32, default="manual")
    # External apply destination surfaced behind the posting's "Apply"
    # button. Resolved by the hold-poller's apply-resolver (Phase 2).
    # Distinct from JobApplication.tracking_url, which is per-user.
    # Named `apply_url` (not `application_url`) to avoid shadowing
    # `active_application_status` — that one is the user's
    # JobApplicationStatus rollup for THIS post, a completely different
    # concept.
    apply_url = models.CharField(max_length=2000, null=True, blank=True)
    # State machine for the resolver:
    #   unknown   — never attempted (default; existing rows backfill here)
    #   resolved  — apply_url is trustworthy
    #   internal  — internal-only flow (LinkedIn Easy Apply etc.);
    #               apply_url stays NULL
    #   failed    — resolver ran but couldn't land on a destination
    #   stale     — was resolved, later health-check returned 4xx/5xx
    apply_url_status = models.CharField(max_length=16, default="unknown")
    apply_url_resolved_at = models.DateTimeField(null=True, blank=True)

    # Whether the posting is still accepting applications, populated
    # from the description by the extractor's text-signal pass (see
    # lib/text_signals.py). null = unknown — historical rows we never
    # scanned default here so we don't lie about state. The list view
    # excludes "closed" by default; per-post UI surfaces a chip only
    # in the closed case.
    #
    # Named `posting_status` (not `application_status`) to avoid
    # collision with `JobApplication.status`, the user's per-
    # application state (Applied / Interview Scheduled / Rejected /
    # ...) — entirely different concept that reads identically to
    # the eye.
    POSTING_STATUS_CHOICES = [
        ("open", "Open"),
        ("closed", "Closed"),
    ]
    posting_status = models.CharField(
        max_length=16,
        choices=POSTING_STATUS_CHOICES,
        null=True,
        blank=True,
        db_index=True,
    )

    # Whether the post is fully fleshed-out and usable. False means
    # "needs (re-)scraping" — used to gate the from-text dedup bypass
    # and the extension popup's Send/Open branching. Replaces the
    # word-count heuristic (_is_thin_description). Three sources flip
    # to False: cc_auto email pipeline creating thin stubs, user
    # clicking "Mark incomplete" on the JP detail page, scrape-graph's
    # ReviewCompleteness rejecting the output. One source flips back
    # to True: a successful scrape attach via parse_scrape.
    complete = models.BooleanField(default=True, db_index=True)

    # Dedupe fields. Populated in save(); never read or written by the
    # apply-resolver. See models/job_post_dedupe.py.
    canonical_link = models.CharField(
        max_length=1000, null=True, blank=True, db_index=True
    )
    content_fingerprint = models.CharField(
        max_length=40, null=True, blank=True
    )
    duplicate_of = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="duplicates",
    )

    # Rolling activity timestamp for cross-platform dedupe. Bumped to
    # ``timezone.now()`` whenever any write path resolves an incoming
    # JobPost shell to this row (canonical_link / fingerprint /
    # apply_url-reciprocity hit, scrape attach, federation merge).
    # ``find_duplicate``'s 30-day fingerprint window queries this column
    # instead of ``created_at`` so a role that keeps being re-seen on a
    # different channel stays "alive" for dedupe past the 30-day mark.
    #
    # Long-tail roles (Allstate JP 1329, 42 days old at the time of the
    # rolling-window enhancement) routinely escape the static 30-day
    # window. Rolling the window off ``last_seen_at`` widens coverage
    # without dragging genuinely-stale roles in — the row only stays
    # eligible for fingerprint match while it keeps being re-seen.
    #
    # Default ``timezone.now`` so freshly-created rows are immediately
    # in-window (matching the prior ``created_at``-based behavior on
    # the create path). Historical rows are backfilled by migration
    # 0097 to ``GREATEST(created_at, max(scrape.created_at))``.
    last_seen_at = models.DateTimeField(default=timezone.now, db_index=True)

    # ActivityPub-aligned per-post visibility. Stores AS2 `audience` URI
    # strings as a JSON list. Default: a fresh `[AS2_PUBLIC]` list per row.
    # Today this field is *latent* — Phase 4's /as-object/ adapter and
    # Outbox dispatch will consult it; nothing in core reads it yet beyond
    # the `is_public()` helper that the frontend mirror reflects. Single-
    # user instance, so we only ship Public vs Private (= empty list) for
    # now; Followers/Unlisted granularity is a future UI addition over the
    # same data shape.
    audience = models.JSONField(default=_default_audience_public, blank=True)

    # Stable identifier for the Career Caddy instance that *originated*
    # this JobPost. Defaults to `settings.CAREER_CADDY_INSTANCE` for rows
    # created locally; federated rows carry the remote instance hostname
    # so the five-clause visibility filter (api/job_hunting/api/views/
    # jobs.py) can exclude them unless the user has opted into that
    # instance's feed. Indexed because the local-only filter runs on
    # every list query. The /as-object/ adapter uses this value as the
    # host portion of the AS2 `id` URI it emits.
    source_instance = models.CharField(
        max_length=255,
        default=_default_source_instance,
        db_index=True,
    )

    class Meta:
        db_table = "job_post"
        indexes = [
            models.Index(
                fields=["content_fingerprint", "-created_at"],
                name="jobpost_fp_recent_idx",
            ),
            # Rolling-window fingerprint dedupe index. ``find_duplicate``
            # queries (content_fingerprint, last_seen_at >= cutoff)
            # ordered by created_at on every JobPost write path; without
            # this composite the planner falls back to seq scan once the
            # table grows beyond the fingerprint-only b-tree's locality.
            models.Index(
                fields=["content_fingerprint", "-last_seen_at"],
                name="jobpost_fp_lastseen_idx",
            ),
        ]

    def save(self, *args, **kwargs):
        # posted_date falls back to today (or created_at's date) whenever
        # the extractor / caller didn't supply one. This matters for list
        # sorting — /job-posts sorts `-posted_date NULLS LAST`, so posts
        # with a null posted_date get buried at the end of the pagination
        # and appear missing from page 1. Paste-sourced posts frequently
        # have no extractable posted_date; without this fallback they'd
        # never surface in "recent posts" views.
        if self.posted_date is None:
            if self.created_at:
                self.posted_date = self.created_at.date()
            else:
                self.posted_date = timezone.now().date()
        # Ember Data serializes unset string @attr as null on createRecord,
        # so the JSON:API POST body carries `apply_url_status: null`. The
        # model default is Python-side only — there is no DB DEFAULT — so
        # an explicit None propagates to INSERT and trips the NOT NULL.
        if self.apply_url_status is None:
            self.apply_url_status = "unknown"
        # Sanitize trailing HTML/markdown delimiter junk that LLM URL
        # extractors leak into the field. See
        # job_post_dedupe.strip_url_trailing_junk — the 2026-05-27
        # hiring.cafe JP 2981 incident is the canonical case.
        self.link = strip_url_trailing_junk(self.link)
        self.apply_url = strip_url_trailing_junk(self.apply_url)
        if self.link and not self.canonical_link:
            self.canonical_link = canonicalize_link(self.link)
        if not self.content_fingerprint:
            self.content_fingerprint = fingerprint(self)
        super().save(*args, **kwargs)

    @property
    def canonical(self):
        """Walk the duplicate chain; return self if not a dupe."""
        return self.duplicate_of.canonical if self.duplicate_of_id else self

    def is_public(self):
        """True iff the AS2 Public collection URI is in `audience`.

        Defensive against historical / malformed values: a non-list (None,
        dict, string from a stray hand-edit) reads as not-public rather
        than raising. Phase 4 federation dispatch will key off this; for
        now it's mirrored to the frontend via the JSON:API serializer so
        the show-page badge can render.
        """
        audience = self.audience
        if not isinstance(audience, list):
            return False
        return AS2_PUBLIC in audience

    # Per-caller triage state (status / reason_code / note) is pre-attached
    # by JobPostViewSet via `_attach_active_application_status` as
    # `_active_application_status`, `_active_reason_code`,
    # `_active_reason_note` — and emitted on the JSON:API response under
    # `meta.triage`, NOT under `attributes`. Not a property of JobPost:
    # JobPost is shared across users, the triage state is per-user. Read
    # the private `_active_*` names directly from the serializer's
    # to_resource override; do not add public `@property`s here, they'd
    # suggest this data belongs on the model row.

    @property
    def top_score(self):
        """Highest integer score value for this job post, or None.

        Request-scoped: views MUST attach `_top_score` filtered by
        `request.user` (Score where user=request.user) so cross-user
        scores don't leak via shared JobPosts. The unscoped fallback
        below runs only in non-request contexts (shell, fixtures).
        """
        if hasattr(self, "_top_score"):
            best = self._top_score
        else:
            best = self.scores.order_by("-score").first()
        return best.score if best is not None else None

    @property
    def top_score_record(self):
        """Score object with the highest score value. Request-scoped — see top_score."""
        if hasattr(self, "_top_score"):
            return self._top_score
        return self.scores.order_by("-score").first()

    @classmethod
    def from_json(cls, job_dict, **kwargs):
        """Create or retrieve a JobPost from a parsed job dict."""
        from job_hunting.models.company import Company
        from job_hunting.models.job_post_dedupe import find_duplicate
        company_data = job_dict.get("company") or {}
        company_name = (
            company_data.get("name") if isinstance(company_data, dict) else company_data
        )
        company = None
        if company_name:
            company, _ = Company.objects.get_or_create(name=company_name)
        candidate = cls(
            title=job_dict.get("title"),
            company=company,
            description=job_dict.get("description"),
            link=job_dict.get("link"),
            location=job_dict.get("location"),
        )
        candidate.canonical_link = canonicalize_link(candidate.link)
        candidate.content_fingerprint = fingerprint(candidate)
        existing = find_duplicate(candidate)
        if existing:
            # Roll the dedupe window forward on this resolution path
            # too — JobPost.from_json is called from chat-server CRUD
            # MCP tools and a handful of legacy import scripts. Bumping
            # here keeps every dedupe-resolves-to-existing entry point
            # symmetric with the views/jobs.py + parse_scrape paths.
            from job_hunting.models.job_post_dedupe import bump_last_seen
            bump_last_seen(existing)
            return existing
        job_post, _ = cls.objects.get_or_create(
            title=candidate.title,
            company=company,
            defaults={
                "description": candidate.description,
                "link": candidate.link,
                "location": candidate.location,
            },
        )
        return job_post
