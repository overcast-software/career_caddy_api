from django.db import models


class JobApplicationStatus(models.Model):
    application = models.ForeignKey(
        "Application",
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
    note = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "job_application_status"
