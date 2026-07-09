from django.conf import settings
from django.db import models

from .base import GetMixin
from .nanoid_pk import NanoIDModel


# Longest text_excerpt we persist. The extension sends the ATS page's
# visible body so the matcher LLM can read the actual application context;
# 8000 chars is enough to carry the title/company/req-id prose without
# blowing the per-request token budget. Truncation happens at write time in
# the viewset so the row itself is the cost guardrail (the matcher never
# re-expands it).
TEXT_EXCERPT_MAX_LEN = 8000

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


class MatchRequest(GetMixin, NanoIDModel):
    """A staff-gated agentic lookup: "find the JobPost this ATS page is for."

    When the ccsender extension can't match the current application page to a
    JobPost deterministically (no source token in the landing URL, referrer
    stripped, SPA fragment routing, the same job reworded across boards), a
    staff user sends the application CONTEXT here — the page URL, the referrer,
    the visible page title, and a text excerpt of the page body. An async
    django-q2 task (``job_hunting.lib.tasks.match_request_job``) pre-fetches
    candidate posts via the existing search legs and makes ONE LLM call that
    picks ``{result_job_post | null, confidence, rationale}`` from that
    candidate list — choose-from-list only, never invent an id. The extension
    polls ``GET /api/v1/match-requests/<id>`` for the terminal result.

    ``id`` is the 10-char NanoID string PK from ``NanoIDModel`` (CC-77). The
    row doubles as the audit trail + dedupe key for repeat asks, so it is
    persisted whether or not a match is found.

    LLM invocation costs tokens per request, which is why the endpoint is
    entitlement-gated (currently ``is_staff``; a per-user entitlement flag is
    the follow-up generalization). ``status`` moves ``pending`` -> ``done`` /
    ``failed`` exactly once; the task never retries the LLM call.
    """

    STATUS_CHOICES = [
        (STATUS_PENDING, "pending"),
        (STATUS_DONE, "done"),
        (STATUS_FAILED, "failed"),
    ]

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="match_requests",
    )
    # The application-page URL the user is standing on. Char (not the model's
    # unique link field) — many requests can point at the same ATS URL, and we
    # want the audit history, not dedup-at-write.
    url = models.CharField(max_length=2000)
    # Where the user arrived FROM. Often the job-board posting whose apply
    # button led here; the strongest single matching signal when present.
    referrer = models.CharField(max_length=2000, blank=True)
    page_title = models.CharField(max_length=500, blank=True)
    # Visible page-body excerpt, truncated to TEXT_EXCERPT_MAX_LEN at write.
    text_excerpt = models.TextField(blank=True)

    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )
    # The chosen JobPost, or NULL when the matcher found no candidate worth
    # picking. SET_NULL so deleting a post doesn't cascade away the audit row.
    result_job_post = models.ForeignKey(
        "JobPost",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="match_requests",
    )
    # 0-1 model-reported confidence in the pick; NULL until the task runs (and
    # on the zero-candidate / failed paths).
    confidence = models.FloatField(null=True, blank=True)
    # One-sentence model rationale, or the safe error summary on failure.
    rationale = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "match_request"
        ordering = ["-created_at", "-id"]
