from django.db import models
from .base import GetMixin
from urllib.parse import urlparse


class Scrape(GetMixin, models.Model):
    url = models.CharField(max_length=2000, null=True, blank=True)
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

    class Meta:
        db_table = "scrape"

    @property
    def host(self):
        if self.url:
            return urlparse(self.url).netloc
        return None
