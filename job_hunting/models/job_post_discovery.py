from django.conf import settings
from django.db import models


class JobPostDiscovery(models.Model):
    job_post = models.ForeignKey(
        "JobPost",
        on_delete=models.CASCADE,
        related_name="discoveries",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="job_post_discoveries",
    )
    source = models.CharField(max_length=32, default="manual")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "job_post_discovery"
        constraints = [
            models.UniqueConstraint(
                fields=["job_post", "user"],
                name="job_post_discovery_unique_user_post",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
        ]
