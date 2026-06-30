from django.conf import settings
from django.db import models
from .base import GetMixin
from .nanoid_pk import NanoIDModel


TIER_CHOICES = [
    ("auto", "Auto"),
    ("0", "Tier 0 — Deterministic"),
    ("1", "Tier 1 — Mini + Hints"),
    ("2", "Tier 2 — Haiku"),
    ("3", "Tier 3 — Sonnet"),
]

# --- known-good readiness tunables -----------------------------------------
# A "known-good" domain is one whose Tier-0 deterministic extraction is
# trustworthy enough that downstream consumers (the email auto-scrape poller
# in automation/, the browser extension in frontend/, the scrape graph in
# agents/) can short-circuit heavier tiers / manual review for that host.
# These are the only thresholds that define the signal — keep them here so the
# definition lives in one place rather than scattered as inline magic numbers.
#
# job_data sub-keys that must all carry a non-empty selector. `company` maps
# to the `company_name` selector key (the field name the Tier-0 BeautifulSoup
# pass reads).
KNOWN_GOOD_REQUIRED_JOB_DATA_FIELDS = ("title", "company_name", "description")
# Tiers a known-good domain is allowed to run at. Tier 0 is deterministic;
# "auto" lets Tier 0 run first and fall back. Anything else (1/2/3) means the
# domain has been demoted off deterministic extraction → not known-good.
KNOWN_GOOD_ALLOWED_TIERS = frozenset({"0", "auto"})
KNOWN_GOOD_MIN_SUCCESS_RATE = 0.8
KNOWN_GOOD_MIN_SCRAPE_COUNT = 3
# Tier-0 miss ratio must stay strictly below this for the domain to qualify.
KNOWN_GOOD_MAX_TIER0_MISS_RATIO = 0.5
# Minimum number of *Tier-0 attempts* (hits + misses) before the CSS-trust
# clause is allowed to fire (BACK-111). Tier-0 trust must be measured over
# attempts that actually ran a deterministic CSS pass — extension-direct /
# paste / email scrapes carry no HTML and never run Tier 0, so they must not
# dilute the signal. Below this floor a host is in its relearn window: not
# enough Tier-0 evidence has accrued, so the CSS-trust clause stays silent and
# readiness falls back to the other clauses (this is what keeps the day-1
# counter reset from causing a fleet-wide known-good outage).
KNOWN_GOOD_MIN_TIER0_ATTEMPTS = 3


class ScrapeProfile(GetMixin, NanoIDModel):
    # ``id`` is the 10-char NanoID string PK from NanoIDModel (CC-77 #79
    # true PK swap). ScrapeProfile is a leaf — nothing FKs to it — so the
    # swap only repoints its own PK.
    hostname = models.CharField(max_length=255, unique=True, db_index=True)
    requires_auth = models.BooleanField(default=False)
    avg_content_length = models.IntegerField(null=True, blank=True)
    success_rate = models.FloatField(default=0.0)
    css_selectors = models.JSONField(null=True, blank=True)
    url_rewrites = models.JSONField(null=True, blank=True)
    # Per-host directions for the apply-destination resolver
    # (ResolveApplyUrl in agents/scrape_graph). Shape:
    #   {
    #     "internal_apply_markers": [".jobs-apply-button--easy-apply", ...],
    #     "apply_link_selectors":   ["a[data-tracking-control-name='apply']", ...],
    #     "apply_button_selectors": ["button.apply-button", ...]
    #   }
    # Resolver tries internal markers first (→ status=internal, no nav),
    # then link selectors (read href, no nav), then button selectors
    # (click + capture page.url). Missing/empty → resolver no-ops.
    apply_resolver_config = models.JSONField(null=True, blank=True)
    # Per-host directions for the browser extension (ccsender) at submit
    # time. Distinct from apply_resolver_config above: that one drives
    # the server-side ResolveApplyUrl node inside the scrape-graph after
    # the hold-poller has fetched the page; this one tells the in-page
    # extension which selectors to query in the active tab and which
    # named decoder to run on the captured href. Shape:
    #   {
    #     "apply_button_selectors":   ["a.jobs-apply-button[href]", ...],
    #     "canonical_link_selectors": ["meta[property=\"og:url\"]"],
    #     "apply_url_decoder":        "linkedin_safety_go" | "passthrough" | null
    #   }
    # The decoder is a named protocol — the extension carries its own
    # registry mapping names to JS functions; the api just ships the
    # name. Missing field / null = the extension falls through to its
    # baked LinkedIn defaults so a fresh install or api outage doesn't
    # break submits.
    extension_selectors = models.JSONField(null=True, blank=True)
    extraction_hints = models.TextField(blank=True, default="")
    page_structure = models.TextField(blank=True, default="")
    last_success_at = models.DateTimeField(null=True, blank=True)
    scrape_count = models.IntegerField(default=0)
    failure_count = models.IntegerField(default=0)
    # Number of Tier-0 passes that RAN and MISSED the required fields.
    tier0_miss_count = models.IntegerField(default=0)
    # Number of Tier-0 passes that RAN and HIT (matched the required fields).
    # Paired with tier0_miss_count, this gives a "Tier-0 attempts" denominator
    # (hits + misses) that excludes HTML-less scrapes (extension-direct / paste
    # / email), which never run a Tier-0 pass. Forward-measured: the BACK-111
    # migration resets tier0_miss_count to 0 alongside this field's 0 default so
    # every host re-accrues real Tier-0 evidence from deploy.
    tier0_hit_count = models.IntegerField(default=0)
    preferred_tier = models.CharField(
        max_length=10, choices=TIER_CHOICES, default="auto"
    )
    enabled = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="created_by",
        related_name="scrape_profiles",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "scrape_profile"

    def __str__(self):
        return self.hostname

    @property
    def effective_tier(self) -> str:
        """The tier this domain would actually run at.

        "0" when preferred_tier is pinned to Tier 0; otherwise the stored
        preferred_tier verbatim (so "auto" surfaces as "auto", a demoted
        "1" surfaces as "1"). Pure/read-only.
        """
        return "0" if self.preferred_tier == "0" else self.preferred_tier

    def readiness(self) -> dict:
        """Debug struct describing whether this domain is known-good.

        Returns ``{"known_good": bool, "tier": str, "reasons": [str, ...]}``.
        ``reasons`` lists every failing clause (empty when known-good), which
        powers the /admin/scrape-profiles debug panel. Pure / read-only — no
        DB writes, no queries; reads only fields already on the row.
        """
        reasons: list[str] = []

        if not self.enabled:
            reasons.append("disabled")

        css = self.css_selectors if isinstance(self.css_selectors, dict) else {}
        job_data = css.get("job_data")
        if not isinstance(job_data, dict):
            job_data = {}
        missing = [
            field
            for field in KNOWN_GOOD_REQUIRED_JOB_DATA_FIELDS
            if not (
                isinstance(job_data.get(field), str) and job_data[field].strip()
            )
        ]
        if missing:
            reasons.append(f"missing job_data selectors: {', '.join(missing)}")

        if self.preferred_tier not in KNOWN_GOOD_ALLOWED_TIERS:
            reasons.append(
                f"preferred_tier={self.preferred_tier!r} not in "
                f"{sorted(KNOWN_GOOD_ALLOWED_TIERS)}"
            )

        success_rate = self.success_rate or 0.0
        if success_rate < KNOWN_GOOD_MIN_SUCCESS_RATE:
            reasons.append(
                f"success_rate={success_rate} < {KNOWN_GOOD_MIN_SUCCESS_RATE}"
            )

        scrape_count = self.scrape_count or 0
        if scrape_count < KNOWN_GOOD_MIN_SCRAPE_COUNT:
            reasons.append(
                f"scrape_count={scrape_count} < {KNOWN_GOOD_MIN_SCRAPE_COUNT}"
            )

        # Tier-0 CSS trust is denominated by Tier-0 *attempts* (hits + misses),
        # NOT scrape_count (BACK-111). HTML-less scrapes (extension-direct /
        # paste / email) bump scrape_count without ever running a Tier-0 pass,
        # so dividing by scrape_count diluted the miss ratio and let a host
        # whose CSS NEVER matches (toptal: 6 misses / 0 hits, but 33 scrapes →
        # 0.18) read as known-good. The clause only fires once enough real
        # Tier-0 evidence has accrued; below the floor the host is relearning
        # and the clause defers to the other readiness signals.
        tier0_hit_count = self.tier0_hit_count or 0
        tier0_miss_count = self.tier0_miss_count or 0
        tier0_attempts = tier0_hit_count + tier0_miss_count
        if tier0_attempts >= KNOWN_GOOD_MIN_TIER0_ATTEMPTS:
            if tier0_hit_count == 0:
                # Tier-0 ran enough times and NEVER once matched — dead CSS.
                reasons.append(
                    f"tier0 never matched: 0 hits / {tier0_attempts} attempts"
                )
            else:
                miss_ratio = tier0_miss_count / tier0_attempts
                if miss_ratio >= KNOWN_GOOD_MAX_TIER0_MISS_RATIO:
                    reasons.append(
                        f"tier0_miss_ratio={miss_ratio:.3f} >= "
                        f"{KNOWN_GOOD_MAX_TIER0_MISS_RATIO} "
                        f"({tier0_miss_count}/{tier0_attempts} attempts)"
                    )

        return {
            "known_good": not reasons,
            "tier": self.effective_tier,
            "reasons": reasons,
        }

    @property
    def is_known_good(self) -> bool:
        """True when every readiness clause holds. See ``readiness()`` for the
        per-clause breakdown. Read-only computed signal — no stored column."""
        return self.readiness()["known_good"]
