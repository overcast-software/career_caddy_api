from django.db import models
from .base import GetMixin
from job_hunting.lib.vetting_reasons import VETTING_REASONS


class JobApplicationStatus(GetMixin, models.Model):
    application = models.ForeignKey(
        "JobApplication",
        on_delete=models.CASCADE,
        related_name="application_statuses",
        db_column="application_id",
    )
    status = models.ForeignKey(
        "Status",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="job_application_statuses",
        db_column="status_id",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    logged_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(null=True, blank=True)
    reason_code = models.CharField(
        max_length=32,
        null=True,
        blank=True,
        choices=VETTING_REASONS,
        db_index=True,
    )

    class Meta:
        db_table = "job_application_status"
