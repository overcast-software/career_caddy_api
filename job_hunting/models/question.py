from django.conf import settings
from django.db import models


class Question(models.Model):
    application = models.ForeignKey(
        "Application",
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
    content = models.TextField(null=True, blank=True)
    favorite = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "question"

    @property
    def job_post(self):
        """Get the job post for this question through application."""
        if self.application_id:
            try:
                if self.application and self.application.job_post_id:
                    return self.application.job_post
            except Exception:
                pass
        return None
