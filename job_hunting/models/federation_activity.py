"""FederationActivity — audit log for inbound + outbound AP activities.

Phase 5c of Plans/ActivityPub Phase 5 — federation proper. Every
verified inbound activity and every outbound activity we send lands a
row here. The log is the paper trail for:

- 5e's federated JobPost ingestion replay (re-process all logged
  ``Create(Note)`` activities once the ingest pipeline lands)
- Debugging delivery failures (``delivery_error`` is the peer's
  response snippet, not just a status code)
- Replay protection (``(direction, activity_id)`` unique constraint
  silently drops dupe deliveries with a 202 — see ``actor_inbox``)

``signature_payload`` stores the verified ``Signature`` header
verbatim so post-hoc forensics (a peer claiming we accepted something
they didn't send) can re-verify the signature offline.
"""
from __future__ import annotations

from django.db import models

from .base import GetMixin


DIRECTION_INBOUND = "inbound"
DIRECTION_OUTBOUND = "outbound"

DIRECTION_CHOICES = [
    (DIRECTION_INBOUND, "Inbound"),
    (DIRECTION_OUTBOUND, "Outbound"),
]


# Activity type buckets — anything not in {Follow, Undo, Accept, Create}
# is logged as Other so future activity types (Like, Announce, Update,
# Delete, Move) leave a trail without forcing a schema change.
ACTIVITY_TYPE_FOLLOW = "Follow"
ACTIVITY_TYPE_UNDO = "Undo"
ACTIVITY_TYPE_ACCEPT = "Accept"
ACTIVITY_TYPE_CREATE = "Create"
ACTIVITY_TYPE_OTHER = "Other"

ACTIVITY_TYPE_CHOICES = [
    (ACTIVITY_TYPE_FOLLOW, "Follow"),
    (ACTIVITY_TYPE_UNDO, "Undo"),
    (ACTIVITY_TYPE_ACCEPT, "Accept"),
    (ACTIVITY_TYPE_CREATE, "Create"),
    (ACTIVITY_TYPE_OTHER, "Other"),
]


DELIVERY_PENDING = "pending"
DELIVERY_ACCEPTED = "accepted"
DELIVERY_REJECTED = "rejected"
DELIVERY_FAILED = "failed"

DELIVERY_STATUS_CHOICES = [
    (DELIVERY_PENDING, "Pending"),
    (DELIVERY_ACCEPTED, "Accepted"),
    (DELIVERY_REJECTED, "Rejected"),
    (DELIVERY_FAILED, "Failed"),
]


class FederationActivity(GetMixin, models.Model):
    """One row per inbound-verified or outbound-sent AP activity."""

    direction = models.CharField(
        max_length=16,
        choices=DIRECTION_CHOICES,
        db_index=True,
    )
    activity_type = models.CharField(
        max_length=32,
        choices=ACTIVITY_TYPE_CHOICES,
        default=ACTIVITY_TYPE_OTHER,
        db_index=True,
    )
    activity_id = models.URLField(
        max_length=512,
        db_index=True,
        help_text=(
            "The activity's ``id`` field — peer-asserted for inbound, ours "
            "for outbound. Combined with ``direction`` for replay dedupe."
        ),
    )
    actor_uri = models.URLField(
        max_length=512,
        db_index=True,
        help_text="The activity's ``actor`` field (who performed the action).",
    )
    target_uri = models.URLField(
        max_length=512,
        null=True,
        blank=True,
        help_text="For Follow/Undo, the ``object`` URI. Nullable.",
    )
    local_user = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="federation_activities",
        help_text="Scoped to a local user when applicable; null for instance-level activities.",
    )
    body = models.TextField(
        help_text="Full activity JSON (canonical text). Source of truth for replay + audit.",
    )
    signature_payload = models.TextField(
        null=True,
        blank=True,
        help_text="Verified Signature header for inbound; null for outbound.",
    )
    received_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the inbound activity arrived. Null for outbound rows.",
    )
    delivered_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the outbound activity successfully delivered. Null for inbound rows or failed outbound.",
    )
    delivery_status = models.CharField(
        max_length=16,
        choices=DELIVERY_STATUS_CHOICES,
        default=DELIVERY_ACCEPTED,
        help_text=(
            "Outbound: pending/accepted/rejected/failed based on peer response. "
            "Inbound: ``accepted`` after signature verification passes."
        ),
    )
    delivery_error = models.TextField(
        null=True,
        blank=True,
        help_text="Status code + body snippet on outbound failure; null otherwise.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "federation_activities"
        constraints = [
            # Replay dedupe — a peer redelivering the same activity_id is
            # silently dropped at the inbox handler before it hits this
            # table; the unique constraint here is the belt to that
            # suspenders so the audit log never grows duplicates.
            models.UniqueConstraint(
                fields=["direction", "activity_id"],
                name="federation_activity_unique_direction_id",
            ),
        ]
        indexes = [
            models.Index(fields=["activity_type", "-created_at"]),
            models.Index(fields=["actor_uri"]),
        ]

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        return f"{self.direction}/{self.activity_type}/{self.activity_id}"
