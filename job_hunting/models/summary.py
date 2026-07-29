from django.conf import settings
from .base import GetMixin
from django.db import models


class Summary(GetMixin, models.Model):
    content = models.TextField(null=True, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="summaries",
    )
    # CC-218: a real FK onto JobPost's NanoID string PK. Was a legacy
    # IntegerField (migration 0006) that CC-57 never converted — so writing a
    # Summary for a real NanoID JobPost rejected the id end-to-end. Mirrors
    # Score/CoverLetter's job_post FK (SET_NULL, nullable). Column stays
    # ``job_post_id`` (Django's FK default), so existing reads are unaffected.
    job_post = models.ForeignKey(
        "JobPost",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="summaries",
    )
    status = models.CharField(max_length=50, null=True, blank=True)

    class Meta:
        db_table = "summary"
