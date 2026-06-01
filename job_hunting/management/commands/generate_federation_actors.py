"""Backfill one Person Actor per existing User (idempotent).

Phase 5a of Plans/ActivityPub Phase 5 — federation proper. After
``bootstrap_instance_actor`` creates the server-level actor, this
command walks the User table and ensures every user has a matching
``Actor`` row (``type=Person``, ``preferred_username=username``).

Skips users whose username collides with the reserved ``instance``
handle. Keypairs are NOT minted here; they generate lazily on first
Actor view hit so the backfill stays fast and re-runnable.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from job_hunting.models import Actor
from job_hunting.models.actor import ACTOR_TYPE_PERSON

from .bootstrap_instance_actor import INSTANCE_USERNAME


User = get_user_model()


class Command(BaseCommand):
    help = "Create a Person Actor for every existing User (idempotent)."

    def handle(self, *args, **options):
        created = 0
        existing = 0
        skipped = 0

        for user in User.objects.all().iterator():
            username = user.username
            if username == INSTANCE_USERNAME:
                # Reserved for the Instance Actor; skip the collision
                # silently so the backfill stays idempotent in mixed
                # databases.
                skipped += 1
                continue

            _, was_created = Actor.objects.get_or_create(
                preferred_username=username,
                defaults={
                    "type": ACTOR_TYPE_PERSON,
                    "user": user,
                },
            )
            if was_created:
                created += 1
            else:
                existing += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"federation actors — created={created} existing={existing} "
                f"skipped={skipped}"
            )
        )
