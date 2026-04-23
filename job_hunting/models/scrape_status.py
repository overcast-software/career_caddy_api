from django.db import models
from .base import GetMixin


class ScrapeStatus(GetMixin, models.Model):
    scrape = models.ForeignKey(
        "Scrape",
        on_delete=models.CASCADE,
        related_name="scrape_statuses",
        db_column="scrape_id",
    )
    status = models.ForeignKey(
        "Status",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scrape_statuses",
        db_column="status_id",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    logged_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(null=True, blank=True)
    # Scrape-graph observability — populated by BaseNode's tracing mixin
    # when the pydantic-graph runner is active. Null for legacy-path
    # ScrapeStatus rows so the new fields don't disturb anything.
    graph_node = models.CharField(max_length=64, null=True, blank=True)
    graph_payload = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "scrape_status"
