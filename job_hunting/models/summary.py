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
    # Temporary plain int until JobPost is migrated to Django
    job_post_id = models.IntegerField(null=True, blank=True)
    status = models.CharField(max_length=50, null=True, blank=True)

    class Meta:
        db_table = "summary"
