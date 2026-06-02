"""Inspect outbound federation dispatch state.

Tabular operator report: counts by delivery_status, oldest pending row,
recently-dead-lettered, top failure hosts. Use during incident response
or as part of a smoke-check after a deploy that touched federation
code.

Usage:

    ./manage.py federation_dispatch_status
    ./manage.py federation_dispatch_status --window-hours 24
"""
from __future__ import annotations

from collections import Counter
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Count
from django.utils import timezone

from job_hunting.models import FederationActivity
from job_hunting.models.federation_activity import (
    DELIVERY_DEAD_LETTER,
    DELIVERY_PENDING,
    DIRECTION_OUTBOUND,
)


class Command(BaseCommand):
    help = "Print outbound FederationActivity dispatch status (counts, oldest pending, dead-letter)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--window-hours",
            type=int,
            default=24,
            help="How far back to consider 'recent' for the dead-letter + failure-host tallies (default 24h).",
        )
        parser.add_argument(
            "--stuck-after-minutes",
            type=int,
            default=60,
            help="Flag pending rows whose next_attempt_at is older than this many minutes (default 60).",
        )

    def handle(self, *args, **options):
        window = timedelta(hours=options["window_hours"])
        stuck_after = timedelta(minutes=options["stuck_after_minutes"])
        now = timezone.now()
        cutoff = now - window

        outbound = FederationActivity.objects.filter(direction=DIRECTION_OUTBOUND)
        total = outbound.count()

        status_counts = (
            outbound.values("delivery_status")
            .annotate(n=Count("pk"))
            .order_by("delivery_status")
        )

        self.stdout.write(self.style.MIGRATE_HEADING("Outbound federation dispatch status"))
        self.stdout.write(f"Total rows: {total}")
        self.stdout.write("By status:")
        for entry in status_counts:
            self.stdout.write(f"  {entry['delivery_status']:>11s}  {entry['n']}")

        oldest_pending = (
            outbound.filter(delivery_status=DELIVERY_PENDING)
            .order_by("next_attempt_at")
            .values_list("pk", "next_attempt_at")
            .first()
        )
        if oldest_pending:
            pk, nxt = oldest_pending
            age = (now - nxt).total_seconds() / 60 if nxt else None
            self.stdout.write(
                f"Oldest pending: row {pk}, next_attempt_at={nxt} (age={age:.1f}m)"
                if age is not None
                else f"Oldest pending: row {pk} (no next_attempt_at set)"
            )
        else:
            self.stdout.write("No pending rows.")

        # "Stuck" pending rows — overdue without a re-enqueue. The sweep
        # picks them up but if it's not running we want to know.
        stuck = outbound.filter(
            delivery_status=DELIVERY_PENDING,
            next_attempt_at__lt=now - stuck_after,
        ).count()
        if stuck:
            self.stdout.write(
                self.style.WARNING(
                    f"Stuck pending (>{options['stuck_after_minutes']}m overdue): {stuck} "
                    "— check that the worker + federation_dispatch_sweep schedule are running."
                )
            )

        dead_recent = outbound.filter(
            delivery_status=DELIVERY_DEAD_LETTER,
            created_at__gte=cutoff,
        ).count()
        self.stdout.write(
            f"Dead-lettered (last {options['window_hours']}h): {dead_recent}"
        )

        # Top failure hosts — peers whose deliveries we've given up on
        # recently. Inferred from target_uri host.
        failure_hosts: Counter = Counter()
        recent_failures = outbound.filter(
            delivery_status=DELIVERY_DEAD_LETTER,
            created_at__gte=cutoff,
        ).values_list("target_uri", flat=True)
        from urllib.parse import urlparse

        for uri in recent_failures:
            host = urlparse(uri or "").netloc.lower()
            if host:
                failure_hosts[host] += 1
        if failure_hosts:
            self.stdout.write("Top failure hosts:")
            for host, count in failure_hosts.most_common(5):
                self.stdout.write(f"  {count:>4d}  {host}")
        else:
            self.stdout.write("No dead-letter activity in the window.")
