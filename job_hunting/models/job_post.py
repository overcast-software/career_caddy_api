from django.conf import settings
from .base import GetMixin
from django.db import models


class JobPost(GetMixin, models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        db_column="created_by",
        related_name="created_job_posts",
    )
    description = models.TextField(null=True, blank=True)
    title = models.CharField(max_length=255, null=True, blank=True)
    company = models.ForeignKey(
        "Company",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="job_posts",
    )
    posted_date = models.DateField(null=True, blank=True)
    extraction_date = models.DateField(null=True, blank=True)
    link = models.CharField(max_length=1000, null=True, blank=True, unique=True)
    salary_min = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    salary_max = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    location = models.CharField(max_length=255, null=True, blank=True)
    remote = models.BooleanField(null=True, blank=True)
    # Provenance: where the post entered the system. Values come from
    # the calling code path (manual, paste, scrape, email, chat, ...);
    # free-form CharField rather than an enum so future sources can
    # appear without migrations. Defaults to 'manual' so historical
    # rows backfill safely.
    source = models.CharField(max_length=32, default="manual")
    # External apply destination surfaced behind the posting's "Apply"
    # button. Resolved by the hold-poller's apply-resolver (Phase 2).
    # Distinct from JobApplication.tracking_url, which is per-user.
    # Named `apply_url` (not `application_url`) to avoid shadowing
    # `active_application_status` — that one is the user's
    # JobApplicationStatus rollup for THIS post, a completely different
    # concept.
    apply_url = models.CharField(max_length=2000, null=True, blank=True)
    # State machine for the resolver:
    #   unknown   — never attempted (default; existing rows backfill here)
    #   resolved  — apply_url is trustworthy
    #   internal  — internal-only flow (LinkedIn Easy Apply etc.);
    #               apply_url stays NULL
    #   failed    — resolver ran but couldn't land on a destination
    #   stale     — was resolved, later health-check returned 4xx/5xx
    apply_url_status = models.CharField(max_length=16, default="unknown")
    apply_url_resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "job_post"

    @property
    def active_application_status(self):
        """Latest JobApplicationStatus.status name for the request user's
        application on this post. Pre-attached by JobPostViewSet for list/
        retrieve; returns None if not pre-attached."""
        return getattr(self, "_active_application_status", None)

    @property
    def top_score(self):
        """Highest integer score value for this job post, or None."""
        best = getattr(self, "_top_score", None) or self.scores.order_by("-score").first()
        return best.score if best is not None else None

    @property
    def top_score_record(self):
        """Score object with the highest score value."""
        return getattr(self, "_top_score", None) or self.scores.order_by("-score").first()

    @classmethod
    def from_json(cls, job_dict, **kwargs):
        """Create or retrieve a JobPost from a parsed job dict."""
        from job_hunting.models.company import Company
        company_data = job_dict.get("company") or {}
        company_name = (
            company_data.get("name") if isinstance(company_data, dict) else company_data
        )
        company = None
        if company_name:
            company, _ = Company.objects.get_or_create(name=company_name)
        job_post, _ = cls.objects.get_or_create(
            title=job_dict.get("title"),
            company=company,
            defaults={
                "description": job_dict.get("description"),
                "link": job_dict.get("link"),
            },
        )
        return job_post
