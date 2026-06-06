"""Read-only audit: list users whose username violates the catchall policy.

`<username>@careercaddy.online` is the Phase 2.5 catchall mailbox. Going
forward, new signups are validated against `lib/username_policy` — this
command surfaces pre-existing rows that pre-date the validator so an
operator can hand-fix them (rename, send a re-onboarding email, etc.).

Read-only by design — no rename, no flag-flip, no email send. The spec
makes this explicit: silent rename of a user account would break their
JWT auth without warning. Operators decide remediation case by case.

Output format: tab-separated `id\tusername\temail`, one row per
violator, sorted by id ASC. Exit status is 0 even when violators are
present (this is an informational tool, not a CI gate).
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from job_hunting.lib.username_policy import UsernamePolicyError, validate_username


class Command(BaseCommand):
    help = (
        "Print users whose username violates the catchall mail "
        "policy (see lib/username_policy.py). Read-only."
    )

    def handle(self, *args, **options):
        User = get_user_model()
        # ID-ordered so the operator can spot-fix by chronological
        # signup order (older accounts likely need an email blast;
        # newer rows are usually staff-seeded test data).
        violators = []
        for u in User.objects.order_by("id").only("id", "username", "email"):
            try:
                validate_username(u.username or "")
            except UsernamePolicyError as exc:
                violators.append((u.id, u.username, u.email or "", str(exc)))

        if not violators:
            self.stdout.write(
                "OK — every username passes the catchall policy."
            )
            return

        self.stdout.write(
            f"{len(violators)} user(s) violate the catchall policy:"
        )
        self.stdout.write("id\tusername\temail\treason")
        for uid, username, email, reason in violators:
            self.stdout.write(f"{uid}\t{username}\t{email}\t{reason}")
