"""Create the Instance Actor row (idempotent).

Phase 5a of Plans/ActivityPub Phase 5 — federation proper. Mastodon's
authorized-fetch mode (default-on in recent versions) refuses outbound
Actor lookups unless the requester signs with a server-level actor key
that belongs to no user. This command creates that row.

Run at deploy time, before any federation traffic. Safe to re-run —
exits cleanly if the Instance Actor already exists. The RSA keypair is
NOT minted here; it's lazily generated on first Actor view hit so the
deploy command stays fast and the encrypted-at-rest decision (Q2) can
be revisited without touching this command.
"""
from django.core.management.base import BaseCommand

from job_hunting.models import Actor
from job_hunting.models.actor import ACTOR_TYPE_APPLICATION


INSTANCE_USERNAME = "instance"


class Command(BaseCommand):
    help = "Create the Instance Actor for ActivityPub federation (idempotent)."

    def handle(self, *args, **options):
        actor, created = Actor.objects.get_or_create(
            preferred_username=INSTANCE_USERNAME,
            defaults={
                "type": ACTOR_TYPE_APPLICATION,
                "user": None,
            },
        )
        if created:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Created Instance Actor (id={actor.pk}, type={actor.type})"
                )
            )
        else:
            self.stdout.write(
                f"Instance Actor already exists (id={actor.pk}, type={actor.type})"
            )
