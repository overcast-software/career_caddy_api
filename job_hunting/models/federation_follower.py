"""FederationFollower — local-user-scoped remote follower record.

Phase 5c of Plans/ActivityPub Phase 5 — federation proper. One row per
(local_user, remote_actor) pair. The FK targets ``User`` rather than
``Actor`` because today every user maps onto exactly one Person actor,
and pinning to the user keeps the row stable across any future Actor
re-keying / re-namespacing (e.g. if a username changes, the
``federation_actors`` row is mutated rather than replaced).

Soft state, not hard delete:

- ``accepted_at`` — when WE sent ``Accept(Follow)`` and the peer 2xx'd.
  ``None`` until the outbound Accept lands; failed Accepts leave this
  ``None`` so 5d's retry pass can pick them up.
- ``unfollowed_at`` — when WE saw ``Undo(Follow)``. ``None`` = currently
  following. Setting this is the "soft delete" — keeps the row around
  for re-follow detection and audit (5e's ingest replay needs to see
  the historical relationship even after an unfollow).

``shared_inbox_uri`` is the Mastodon-style optimization for fan-out:
when present, 5d dispatch can batch one POST per peer instance instead
of one per follower. Not consulted for ``Accept(Follow)`` — that goes
to the per-actor inbox because the activity is addressed to a single
actor, not to a collection.
"""
from __future__ import annotations

from urllib.parse import urlparse

from django.conf import settings
from django.db import models

from .base import GetMixin


class FederationFollower(GetMixin, models.Model):
    """Remote actor following one of our local actors."""

    local_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="federation_followers",
        help_text="Local user being followed (the followee).",
    )
    actor_uri = models.URLField(
        max_length=512,
        help_text="Remote follower's Actor URI (e.g. https://mastodon.social/users/alice).",
    )
    inbox_uri = models.URLField(
        max_length=512,
        help_text="Remote actor's per-actor inbox (used for Accept + targeted deliveries).",
    )
    shared_inbox_uri = models.URLField(
        max_length=512,
        null=True,
        blank=True,
        help_text=(
            "Optional shared inbox URL from the remote actor's endpoints. "
            "5d dispatch coalesces fan-out to shared inbox when present."
        ),
    )
    instance_host = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Host portion of actor_uri — indexed for per-instance rate limiting + dispatch coalescing.",
    )
    accepted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the outbound Accept(Follow) successfully delivered.",
    )
    unfollowed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When Undo(Follow) was received. Null = currently following.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "federation_followers"
        constraints = [
            models.UniqueConstraint(
                fields=["local_user", "actor_uri"],
                name="federation_follower_unique_local_remote",
            ),
        ]
        indexes = [
            models.Index(fields=["instance_host"]),
            models.Index(fields=["unfollowed_at"]),
        ]

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        state = "active" if self.unfollowed_at is None else "unfollowed"
        return f"{self.actor_uri} -> {self.local_user_id} ({state})"

    @staticmethod
    def host_for_uri(uri: str) -> str:
        """Extract the host portion of an actor URI for ``instance_host``.

        Lowercased so per-instance lookups don't drift on case (DNS is
        case-insensitive). Falls back to the empty string on malformed
        input rather than raising — the inbox handler validates URIs
        upstream, so a row with an empty host is a bug worth surfacing
        but not worth crashing the request for.
        """
        parsed = urlparse(uri)
        return (parsed.netloc or "").lower()
