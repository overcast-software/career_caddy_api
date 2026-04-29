from django.conf import settings
from django.db import models
from .base import GetMixin
from urllib.parse import urlparse


class Scrape(GetMixin, models.Model):
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
    # Phase 3 learning loop: when the resolver ends in unknown/failed,
    # the ai-side heuristic scan stores candidate "Apply" elements
    # here for later aggregation + promotion into
    # ScrapeProfile.apply_resolver_config. Shape:
    #   [{"selector": ".btn.apply", "href": "https://...", "text": "Apply",
    #     "tag": "a", "score": 0.8, "reason": "href contains 'apply'"}]
    # Capture-only — promotion is a separate manual/automated step.
    apply_candidates = models.JSONField(null=True, blank=True)

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
