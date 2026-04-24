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
