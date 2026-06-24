"""Read-only audit: list users whose username violates the username policy.

The username is both the `<username>@careercaddy.online` catchall
local-part (Phase 2.5) and the public ActivityPub actor handle (CC-56
#58/#59). New signups are validated against `lib/username_policy`; this
command surfaces pre-existing rows that pre-date — or pre-date the
tightening of — the validator (too short, or containing now-disallowed
characters such as `.`/`-`) so an operator can hand-fix them (rename,
send a re-onboarding email, etc.). The per-row `reason` column carries
the exact failure (length vs charset), so the same command covers both
CC-56 #58 (min length) and #59 (charset).

Read-only by design — no rename, no flag-flip, no email send. Existing
handles may already be federated; a silent rename would break their JWT
auth (and any live actor URI) without warning. Operators decide
remediation case by case.

Output format: tab-separated `id\tusername\temail\treason`, one row per
violator, sorted by id ASC. Exit status is 0 even when violators are
present (this is an informational tool, not a CI gate).
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from job_hunting.lib.username_policy import UsernamePolicyError, validate_username


class Command(BaseCommand):
    help = (
        "Print users whose username violates the username policy — "
        "catchall mail local-part + ActivityPub actor handle "
        "(see lib/username_policy.py). Read-only."
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
