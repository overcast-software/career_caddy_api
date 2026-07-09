from django.conf import settings
from .base import GetMixin
from .nanoid_pk import NanoIDModel
from django.db import models


# --- Agentic JobPost match (CC-135, folded into JobApplication) ----------
#
# When the ccsender extension can't match a live application page to a
# JobPost deterministically (no source token in the landing URL, referrer
# stripped, SPA fragment routing, the same job reworded across boards), a
# staff user sends the application CONTEXT with the JobApplication itself —
# the user DID apply, so the JA is created up front and IS the poll target.
# One nullable JSON column (``match_context``) carries the request inputs and
# the async matcher's outputs; there is no separate model.
#
# ``match_context`` shape::
#
#     {
#       # inputs (written by the create path from the request)
#       "referrer": "<job-board posting the apply button came from>",
#       "page_title": "<visible ATS page title>",
#       "text_excerpt": "<page-body excerpt, <= MATCH_TEXT_EXCERPT_MAX_LEN>",
#       # outputs (written by job_application_match_job)
#       "status": "pending" | "done" | "failed",
#       "confidence": <0-1 float | null>,
#       "rationale": "<one-sentence model rationale, or safe error summary>",
#       "requested_at": "<iso8601, set at write>",
#       "finished_at": "<iso8601 | null, set when terminal>",
#     }
#
# The application-page URL lives in the existing ``tracking_url`` field; the
# match ANSWER lands in the existing ``job_post`` FK (already nullable). A
# null pick is an honest unlinked JA, fixable via the existing link-page tool.
#
# Longest text_excerpt we persist. The extension sends the ATS page's visible
# body so the matcher LLM can read the actual application context; 8000 chars
# carries the title/company/req-id prose without blowing the per-request token
# budget. Truncation happens at write time (the create path) so the row itself
# is the cost guardrail — the matcher never re-expands it.
MATCH_TEXT_EXCERPT_MAX_LEN = 8000

MATCH_STATUS_PENDING = "pending"
MATCH_STATUS_DONE = "done"
MATCH_STATUS_FAILED = "failed"


class JobApplication(GetMixin, NanoIDModel):
    # ``id`` is the 10-char NanoID string PK from NanoIDModel (CC-77 #79
    # true PK swap). Two FKs reference job_application(id), both via
    # db_column="application_id": job_application_status.application_id
    # (CASCADE, NOT NULL) and question.application_id (SET_NULL, nullable).
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applications",
    )
    job_post = models.ForeignKey(
        "JobPost",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applications",
    )
    company = models.ForeignKey(
        "Company",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applications",
    )
    resume = models.ForeignKey(
        "Resume",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applications",
    )
    cover_letter = models.ForeignKey(
        "CoverLetter",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="application",
    )
    applied_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=100, null=True, blank=True)
    tracking_url = models.CharField(max_length=2000, null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
    # Agentic JobPost-match request context + result (CC-135). NULL on the
    # ordinary JA that was never a match trigger; a dict (see the module
    # docstring for the shape) once the extension asks Career Caddy to find
    # the JobPost this ATS page belongs to. The matcher backfills the
    # ``job_post`` FK directly and records its outputs here for the poll.
    match_context = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = "job_application"
