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

    def __str__(self) -> str:  # pragma: no cover - admin/debug only
        return f"{self.type}/{self.preferred_username}"

    @property
    def has_keypair(self) -> bool:
        return bool(self.public_key_pem and self.private_key_pem)
