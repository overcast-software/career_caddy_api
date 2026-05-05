from django.conf import settings
from django.db import models


class JobPostOverwriteDecision(models.Model):
    """Audit row written when a higher-trust source overwrites a lower-
    trust JobPost on a canonical_link / fingerprint collision.

    Companion to :class:`JobPostDescriptionDecision` — that one records
    arbiter calls on competing descriptions; this one records the
    blanket trust-rank overwrite path that bypasses the arbiter.
    """

    job_post = models.ForeignKey(
        "JobPost",
        on_delete=models.CASCADE,
        related_name="overwrite_decisions",
    )
    triggering_scrape = models.ForeignKey(
        "Scrape",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="overwrite_decisions",
    )
    previous_source = models.CharField(max_length=32, blank=True, default="")
    new_source = models.CharField(max_length=32, blank=True, default="")
    # {field_name: {"before": <repr>, "after": <repr>}} for every field
    # the overwrite actually changed. Stored as JSON for cheap diffing
    # without joining a side table; field count is small.
    changed_fields = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="triggered_overwrite_decisions",
    )

    class Meta:
        db_table = "job_post_overwrite_decision"
        indexes = [
            models.Index(fields=["job_post", "-created_at"]),
        ]
