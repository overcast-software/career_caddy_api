"""FederationFollower — followee-scoped remote follower record.

Phase 5c of Plans/ActivityPub Phase 5 — federation proper. One row per
(followee, remote_actor) pair, where the followee is identified by
``local_user`` (Person actor), ``company`` (Organization actor), or
both (a local user subscribing to a Company actor — the Phase 6b
discovery channel for inbound JP ingest). A DB check constraint
enforces "at least one followee is set"; rows that set both
participate in both per-column partial unique indexes.

Phase 6b — Company-actor Follow handshake. Before 6b, only Person
actors had a working Follow path; Company actors had no inbox so
discoveries never materialized. 6b adds the `company` FK so a Follow
against ``/companies/<slug>/`` lands a row keyed off the Company and
the Phase 6b ingest helper can resolve local followers (rows where
both ``company`` and ``local_user`` are set) for the discovery write.

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
from django.db.models import Q

from .base import GetMixin


class FederationFollower(GetMixin, models.Model):
    """Remote actor following one of our local actors."""

    local_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="federation_followers",
        help_text=(
            "Local user being followed (Person-actor followee), OR the "
            "local user subscribing to a Company actor (when ``company`` "
            "is also set). NULL when the followee is a Company alone."
        ),
    )
    company = models.ForeignKey(
        "Company",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="federation_followers",
        help_text=(
            "Phase 6b — Company being followed (Organization-actor followee). "
            "NULL for pure Person-actor follows. May co-exist with "
            "``local_user`` when a local user subscribes to a Company "
            "(the discovery channel for inbound JP ingest)."
        ),
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
            # Phase 6b — at least one followee identity. A row is keyed
            # off ``local_user`` (Person-actor followee), ``company``
            # (Organization-actor followee), or BOTH (a local user
            # subscribing to a Company actor — the discovery channel for
            # Phase 6b inbound JP ingest). The constraint refuses
            # ``(NULL, NULL)`` rows since they carry no followee identity.
            models.CheckConstraint(
                condition=(
                    Q(local_user__isnull=False) | Q(company__isnull=False)
                ),
                name="federation_follower_followee_required",
            ),
            # Two partial unique indexes — one per followee column. The
            # Person-actor path's existing unique invariant carries
            # through; the Company-actor path gets its own. Rows that
            # set BOTH columns participate in both indexes, which is
            # exactly what we want: a local user following a Company is
            # unique by ``(local_user, actor_uri)`` AND by
            # ``(company, actor_uri)``.
            models.UniqueConstraint(
                fields=["local_user", "actor_uri"],
                condition=Q(local_user__isnull=False),
                name="federation_follower_unique_local_remote",
            ),
            models.UniqueConstraint(
                fields=["company", "actor_uri"],
                condition=Q(company__isnull=False),
                name="federation_follower_unique_company_remote",
            ),
        ]
        indexes = [
            models.Index(fields=["instance_host"]),
            models.Index(fields=["unfollowed_at"]),
            models.Index(fields=["company"]),
        ]

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        state = "active" if self.unfollowed_at is None else "unfollowed"
        followee = (
            f"user={self.local_user_id}" if self.local_user_id is not None
            else f"company={self.company_id}"
        )
        return f"{self.actor_uri} -> {followee} ({state})"

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
