from django.conf import settings
from django.db import models
from .base import GetMixin


TIER_CHOICES = [
    ("auto", "Auto"),
    ("0", "Tier 0 — Deterministic"),
    ("1", "Tier 1 — Mini + Hints"),
    ("2", "Tier 2 — Haiku"),
    ("3", "Tier 3 — Sonnet"),
]


class ScrapeProfile(GetMixin, models.Model):
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
    tier0_miss_count = models.IntegerField(default=0)
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
