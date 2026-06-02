from django.conf import settings
from django.db import models


class DuplicateAnnotation(models.Model):
    """Audit row for every manual edit to JobPost.duplicate_of.

    One row per verb invocation (mark-duplicate-of / unlink-duplicate /
    promote-canonical). Captures the actor, the before/after state, and
    a frozen snapshot of compute_duplicate_candidates' signals at the
    moment the human made the call. The signal snapshot is what makes
    this corpus useful: it lets the dedupe-feedback report ask
    questions like "which manual marks fired with no automatic signal"
    (gap in find_duplicate) and "which manual unlinks fired despite
    canonical_link matching" (over-eager canonicalization).
    """

    MARK = "mark"
    UNLINK = "unlink"
    PROMOTE = "promote"
    HISTORICAL = "historical"
    # Phase 5e — written when an inbound federated Create(Note) merges to
    # an existing local JobPost (via canonical_link match in the 5e
    # ingest decision tree). ``set_by`` is NULL on these rows — there's
    # no local user behind the decision; ``signal_state`` carries the
    # remote actor + activity id so the dedupe-feedback report can pivot
    # on federated-origin merges.
    FEDERATED_MERGE = "federated_merge"

    ACTIONS = [
        (MARK, "mark"),
        (UNLINK, "unlink"),
        (PROMOTE, "promote"),
        (HISTORICAL, "historical"),
        (FEDERATED_MERGE, "federated_merge"),
    ]

    from_jp = models.ForeignKey(
        "JobPost",
        on_delete=models.CASCADE,
        related_name="duplicate_annotations",
    )
    to_jp = models.ForeignKey(
        "JobPost",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    previous_to = models.ForeignKey(
        "JobPost",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    action = models.CharField(max_length=16, choices=ACTIONS)
    set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="duplicate_annotations",
    )
    set_at = models.DateTimeField(auto_now_add=True)
    signal_state = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "duplicate_annotation"
        indexes = [
            models.Index(fields=["from_jp", "-set_at"]),
            models.Index(fields=["action", "-set_at"]),
        ]
