from django.conf import settings
from django.db import models


class Question(models.Model):
    application_id = models.IntegerField(null=True, blank=True)  # temp until Application migrated
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
                from job_hunting.lib.models.application import Application
                from job_hunting.lib.models.base import BaseModel
                session = BaseModel.get_session()
                app = session.query(Application).filter_by(id=self.application_id).first()
                if app:
                    return app.job_post
            except Exception:
                pass
        return None
