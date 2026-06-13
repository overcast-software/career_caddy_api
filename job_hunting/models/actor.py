"""ActivityPub Actor model.

Phase 5a of Plans/ActivityPub Phase 5 — federation proper. A separate
``Actor`` row owns federation surface (preferredUsername, publicKey,
privateKey, type). Augmenting ``User`` was the original sketch; Q1
(resolved 2026-06-01) settled on this separate-model shape because:

- The Instance Actor — mandatory for Mastodon's authorized-fetch mode —
  has no associated ``User``. ``Actor.user`` is therefore nullable.
- Future Service / Group / Application / Organization actors (Company
  pages, cc_auto-the-app, moderation actors) also don't map onto
  ``User``. ``Actor.type`` enumerates the AS2 vocabulary.
- ``User`` keeps auth + identity; ``Actor`` owns federation.

Phase 5a builds the model + WebFinger + Actor view + lazy keypair
generation only. Outbox (5b), Inbox + Follow + HTTP Signatures (5c),
outbound dispatch (5d), and federated ingest (5e) follow.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from .base import GetMixin


# AS2 vocabulary actor types — https://www.w3.org/TR/activitystreams-vocabulary/#actor-types
ACTOR_TYPE_PERSON = "Person"
ACTOR_TYPE_SERVICE = "Service"
ACTOR_TYPE_GROUP = "Group"
ACTOR_TYPE_APPLICATION = "Application"
ACTOR_TYPE_ORGANIZATION = "Organization"

ACTOR_TYPE_CHOICES = [
    (ACTOR_TYPE_PERSON, "Person"),
    (ACTOR_TYPE_SERVICE, "Service"),
    (ACTOR_TYPE_GROUP, "Group"),
    (ACTOR_TYPE_APPLICATION, "Application"),
    (ACTOR_TYPE_ORGANIZATION, "Organization"),
]


class Actor(GetMixin, models.Model):
    """ActivityPub Actor — one row per federation identity.

    The ``user`` FK is nullable: the Instance Actor (and any future
    Service / Application actors) carry no associated ``User``. For
    Person actors, ``preferred_username`` mirrors ``user.username``;
    for the Instance Actor it's the reserved name ``instance``.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="federation_actors",
    )
    # Phase 6a — Organization actors. A Company-actor row carries
    # ``company`` set + ``user`` NULL; a Person actor carries ``user``
    # set + ``company`` NULL; the Instance Actor carries both NULL.
    # The mutual-exclusivity check below enforces "at most one set."
    company = models.ForeignKey(
        "Company",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="federation_actors",
    )
    type = models.CharField(
        max_length=32,
        choices=ACTOR_TYPE_CHOICES,
        default=ACTOR_TYPE_PERSON,
    )
    preferred_username = models.SlugField(
        max_length=150,
        unique=True,
        help_text=(
            "WebFinger / Actor URI handle. Person actors mirror "
            "User.username; Instance Actor uses 'instance'."
        ),
    )
    # Phase 6a — optional icon (Organization logo / Person avatar) URL.
    # Reused by Phase 7a's profile-editor UI for Person actors.
    avatar_url = models.URLField(max_length=500, null=True, blank=True)
    public_key_pem = models.TextField(null=True, blank=True)
    # TODO(Q2): private_key_pem stored plaintext for Phase 5a. Revisit
    # before prod-grade federation rolls out — see notes.org
    # "Plans/ActivityPub Phase 5 — federation proper/Open questions/Q2"
    # for the encrypted-at-rest decision. Keypair is lazily generated
    # on first Actor view hit; no plaintext touches disk on a fresh
    # row until then.
    private_key_pem = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "federation_actors"
        constraints = [
            # Phase 6a mutual-exclusivity: a row is at most one of
            # (Person, Organization). Instance Actor has both NULL.
            # Modeled as a CheckConstraint (not a partial unique) so the
            # database refuses any future write path that sets both
            # columns — the application code only writes one or the other,
            # but a constraint here pins the invariant in the schema.
            models.CheckConstraint(
                condition=(
                    Q(user__isnull=True) | Q(company__isnull=True)
                ),
                name="actor_user_company_mutually_exclusive",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        return f"{self.type}/{self.preferred_username}"

    @property
    def has_keypair(self) -> bool:
        return bool(self.public_key_pem and self.private_key_pem)
