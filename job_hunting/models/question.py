from django.conf import settings
from .base import GetMixin
from django.db import models


class Question(GetMixin, models.Model):
    application = models.ForeignKey(
        "JobApplication",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="questions",
        db_column="application_id",
    )
    company = models.ForeignKey(
        "Company",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="questions",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="created_by_id",
        related_name="questions",
    )
    job_post = models.ForeignKey(
        "JobPost",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="direct_questions",
    )
    content = models.TextField(null=True, blank=True)
    favorite = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "question"
