from django.conf import settings
from .base import GetMixin
from django.db import models


class Score(GetMixin, models.Model):
    score = models.IntegerField(null=True, blank=True)
    status = models.CharField(max_length=50, null=True, blank=True)
    explanation = models.TextField(null=True, blank=True)
    resume = models.ForeignKey(
        "Resume",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scores",
    )
    job_post = models.ForeignKey(
        "JobPost",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scores",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scores",
    )

    class Meta:
        db_table = "score"

    @property
    def company(self):
        if self.job_post_id:
            try:
                return self.job_post.company
            except Exception:
                return None
        return None
