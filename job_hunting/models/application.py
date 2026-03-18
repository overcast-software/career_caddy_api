from django.conf import settings
from django.db import models


class JobApplication(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applications",
    )
    job_post = models.ForeignKey(
        "JobPost",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applications",
    )
    company = models.ForeignKey(
        "Company",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applications",
    )
    resume = models.ForeignKey(
        "Resume",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applications",
    )
    cover_letter = models.ForeignKey(
        "CoverLetter",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="application",
    )
    applied_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=100, null=True, blank=True)
    tracking_url = models.CharField(max_length=2000, null=True, blank=True)
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "application"
