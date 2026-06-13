"""Inspect inbound federation ingestion state.

Sibling of ``federation_dispatch_status`` (5d). Tabular operator
report: counts by ingest outcome over the configured window, top
instances by ingest volume, top instances by reject count. Use during
incident response (peer flooding our quota) or as part of a smoke
check after a deploy that touched ``lib/federation_ingest``.

Outcomes are read off the inbound FederationActivity rows'
``delivery_status`` — 5c lands rows with ``accepted`` after signature
verification, 5e mutates that to ``rejected`` (with a human-readable
``delivery_error`` payload) on its content-level rejects. Rows that
finished as ``created`` / ``merged`` / ``skipped`` keep their
``accepted`` status — those outcomes are inferred from the existence
of a corresponding JobPost.source_instance/source row OR (for skipped)
from the audit row alone. Counts here therefore split into:

  * accepted-and-ingested: row in window where a JobPost row with
    matching source_instance + recent created_at exists.
  * accepted-and-merged: row in window where a DuplicateAnnotation
    row with action=federated_merge references the activity_id.
  * accepted-and-skipped: row in window with neither of the above.
  * rejected: row in window with delivery_status='rejected'.

Usage:

    ./manage.py federation_ingest_status
    ./manage.py federation_ingest_status --window-hours 24
"""
from __future__ import annotations

from collections import Counter
from datetime import timedelta
from urllib.parse import urlparse

from django.core.management.base import BaseCommand
from django.db.models import Count
from django.utils import timezone

from job_hunting.models import (
    DuplicateAnnotation,
    FederationActivity,
    JobPost,
)


class Command(BaseCommand):
    help = (
        "Print inbound FederationActivity ingestion status — counts by "
        "outcome (created / merged / rejected / skipped), top ingesting "
        "peers, top rejecting peers."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--window-hours",
            type=int,
            default=24,
            help="How far back to consider 'recent' (default 24h).",
        )

    def handle(self, *args, **options):
        window = timedelta(hours=options["window_hours"])
        now = timezone.now()
        cutoff = now - window

        inbound = FederationActivity.objects.filter(
            direction="inbound",
            activity_type="Create",
            created_at__gte=cutoff,
        )
        total = inbound.count()

        rejected = inbound.filter(delivery_status="rejected").count()
        accepted = inbound.filter(delivery_status="accepted").count()

        # Created: a JobPost row with source="federation" was created
        # inside the window. We don't have a direct FK back to the
        # activity row, so we count by JobPost; this is approximate
        # (multi-activity-per-jp doesn't double-count, which is the
        # desired behavior anyway). Phase 6b standardised the source
        # label (was ``"activitypub"`` under 5e — see migration 0107).
        created = JobPost.objects.filter(
            source="federation",
            created_at__gte=cutoff,
        ).count()

        # Merged: DuplicateAnnotation rows with the 5e action in the
        # window.
        merged = DuplicateAnnotation.objects.filter(
            action=DuplicateAnnotation.FEDERATED_MERGE,
            set_at__gte=cutoff,
        ).count()

        # Skipped: accepted rows minus the created+merged subset. This
        # is a lower-bound estimate (created/merged counts are global,
        # not joined back to the activity row), but it's the right
        # operator signal for "how many federated creates fell through
        # the no-op skipped branch".
        skipped = max(0, accepted - created - merged)

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"Inbound Create(Note) ingest status — last {options['window_hours']}h"
            )
        )
        self.stdout.write(f"Total activities: {total}")
        self.stdout.write(f"  created:  {created}")
        self.stdout.write(f"  merged:   {merged}")
        self.stdout.write(f"  rejected: {rejected}")
        self.stdout.write(f"  skipped:  {skipped} (approx)")

        # Top ingesting peers — host count from actor_uri on inbound
        # rows in the window. Catches "peer X sent us a flood".
        peer_volume: Counter = Counter()
        for actor_uri in inbound.values_list("actor_uri", flat=True):
            host = urlparse(actor_uri or "").netloc.lower()
            if host:
                peer_volume[host] += 1
        if peer_volume:
            self.stdout.write("Top ingesting peers:")
            for host, count in peer_volume.most_common(5):
                self.stdout.write(f"  {count:>4d}  {host}")

        # Top reject reasons — split out the verdict suffix the
        # ingest module writes into delivery_error so operators can
        # see whether sticky_closed_local or content_too_large is the
        # leading edge.
        rejected_qs = inbound.filter(delivery_status="rejected").values_list(
            "delivery_error", flat=True
        )
        reason_counts: Counter = Counter()
        for err in rejected_qs:
            if not err:
                continue
            reason = err.split("ingest_rejected:", 1)[-1].strip()
            # Collapse schema:loc:msg into schema:loc so we don't
            # fragment counts across pydantic error message variants.
            if reason.startswith("schema:"):
                parts = reason.split(":")
                reason = ":".join(parts[:2])
            reason_counts[reason] += 1
        if reason_counts:
            self.stdout.write("Top reject reasons:")
            for reason, count in reason_counts.most_common(5):
                self.stdout.write(f"  {count:>4d}  {reason}")

        # Top reject peers — same window, hosts only on rejected rows.
        reject_peers: Counter = Counter()
        for actor_uri in inbound.filter(
            delivery_status="rejected"
        ).values_list("actor_uri", flat=True):
            host = urlparse(actor_uri or "").netloc.lower()
            if host:
                reject_peers[host] += 1
        if reject_peers:
            self.stdout.write("Top reject peers:")
            for host, count in reject_peers.most_common(5):
                self.stdout.write(f"  {count:>4d}  {host}")

        # By-type tally of unverified-by-us activity types (for the
        # forward-compat awareness): how many inbound Other types
        # we saw recently. This isn't a 5e responsibility per se but
        # the command is the natural surface for federation operators
        # checking the overall inbound mix.
        other_total = (
            FederationActivity.objects.filter(
                direction="inbound",
                activity_type="Other",
                created_at__gte=cutoff,
            )
            .values("activity_type")
            .annotate(n=Count("pk"))
        )
        for entry in other_total:
            self.stdout.write(f"Inbound Other (forward-compat): {entry['n']}")
