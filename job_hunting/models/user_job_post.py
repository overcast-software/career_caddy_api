from django.conf import settings
from django.db import models


class UserJobPost(models.Model):
    """Owner↔post join (BACK-105, AUTO-18 multi-user forward@).

    Records the OWNER of a JobPost — the recipient-resolved user when
    cc_auto (a STAFF api key) ingests job mail forwarded to
    ``<username>@careercaddy.online``. This is deliberately SEPARATE from
    ``JobPost.created_by``: ``created_by`` stays the author/staff principal
    that drove the write (the api forces it to the authenticated principal
    in ``pre_save_payload`` — correct, unchanged), while this row captures
    *who the post is for*. There is no owner column on JobPost today
    (created_by has been conflating author+owner); this join carries it.

    Distinct from ``JobPostDiscovery`` too: discovery is the per-user
    *visibility signal* (created/applied/scored/scraped/discovered), whereas
    ownership is the stronger claim that drives multi-user forward@ routing.
    The create-path records both — an owner is also a discoverer.

    Server-internal for v1: NOT exposed as a REST resource/serializer.
    """

    job_post = models.ForeignKey(
        "JobPost",
        on_delete=models.CASCADE,
        related_name="user_memberships",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="user_job_posts",
    )

    ROLE_OWNER = "owner"
    ROLE_MEMBER = "member"
    ROLE_CHOICES = (
        (ROLE_OWNER, "owner"),
        (ROLE_MEMBER, "member"),
    )
    role = models.CharField(
        max_length=16,
        default=ROLE_OWNER,
        choices=ROLE_CHOICES,
    )
    # Provenance of the membership (mirrors JobPostDiscovery.source —
    # e.g. "email-forward", "paste", "scrape"). Nullable: callers that
    # don't carry a source still get a clean ownership row.
    source = models.CharField(max_length=32, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "user_job_post"
        unique_together = (("job_post", "user"),)
