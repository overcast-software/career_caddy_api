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
    # Phase 2.5 catchall mail ingest provenance. When `source == "email-forward"`,
    # this records the catchall To-address the user forwarded the listing to
    # (e.g. "dough@careercaddy.online"). Required for that source, null for
    # every other source. Surfaced through the JSON:API `discoveries` hasMany
    # so the UI can render "you forwarded this via <address>" provenance and
    # so reports can audit which mailbox a row entered through. 254 chars
    # matches RFC 5321's max address length.
    forwarded_via_address = models.CharField(max_length=254, null=True, blank=True)
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
