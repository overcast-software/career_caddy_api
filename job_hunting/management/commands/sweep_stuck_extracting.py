"""Flip Scrapes stuck in extracting/updating_profile to failed.

Companion safety net for ``parse_scrape``'s try/finally — handles the
rare cases where finally itself can't run (container OOM-kill, hard
SIGKILL during deploy churn, hardware reboot mid-flight) so a row would
otherwise sit in ``extracting`` forever and hang the user's UI on
``scrape.status`` polling.

Cutoff is computed against the latest ``ScrapeStatus.logged_at`` row
for each stuck scrape, NOT ``Scrape``'s own timestamp (the model has
no ``updated_at`` field — status flips happen via raw ``UPDATE`` so
``auto_now`` would not have fired anyway).

Usage::

    python manage.py sweep_stuck_extracting --min-age-minutes=15
    python manage.py sweep_stuck_extracting --min-age-minutes=15 --dry-run
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Max
from django.utils import timezone

from job_hunting.models import Scrape


STUCK_STATES = ("extracting", "updating_profile")


class Command(BaseCommand):
    help = "Flip Scrapes stuck in extracting/updating_profile to failed."

    def add_arguments(self, parser):
        parser.add_argument("--min-age-minutes", type=int, default=15)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, min_age_minutes, dry_run, **opts):
        from job_hunting.lib.scraper import _log_scrape_status

        cutoff = timezone.now() - timedelta(minutes=min_age_minutes)
        stuck = (
            Scrape.objects
            .filter(status__in=STUCK_STATES)
            .annotate(last_logged=Max("scrape_statuses__logged_at"))
            .filter(last_logged__lt=cutoff)
        )
        flipped = 0
        for s in stuck:
            self.stdout.write(
                f"scrape={s.id} status={s.status} last_logged={s.last_logged}"
            )
            if not dry_run:
                _log_scrape_status(
                    s.id,
                    "failed",
                    note=f"swept: stuck in {s.status} > {min_age_minutes}m",
                    failure_reason=(
                        f"Swept by sweep_stuck_extracting: stuck in "
                        f"{s.status!r} > {min_age_minutes}m without progress"
                    ),
                )
                flipped += 1
        verb = "would flip" if dry_run else "flipped"
        self.stdout.write(f"{verb} {flipped} stuck scrapes")
