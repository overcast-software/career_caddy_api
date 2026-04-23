"""Dump scrape-graph traces as JSONL for offline evaluation.

Each line = one (graph_node transition, terminal outcome) pair. Treat
transitions whose scrape ended in DuplicateShortCircuit / ResolveApplyUrl
as positive labels; ExtractFail / ObstacleFail as negative; still-running
scrapes are skipped.

Usage:
    python manage.py dump_graph_traces --since 7d --format jsonl > traces.jsonl
    python manage.py dump_graph_traces --since 2026-04-20 --format jsonl
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone as dt_timezone

from django.core.management.base import BaseCommand
from django.utils import timezone

from job_hunting.models.scrape_status import ScrapeStatus


TERMINAL_SUCCESS = {"DuplicateShortCircuit", "ResolveApplyUrl"}
TERMINAL_FAILURE = {"ExtractFail", "ObstacleFail"}


class Command(BaseCommand):
    help = "Dump scrape-graph transitions with terminal labels as JSONL."

    def add_arguments(self, parser):
        parser.add_argument("--since", default="7d", help="e.g. '7d', '24h', or ISO date")
        parser.add_argument("--format", default="jsonl", choices=["jsonl"])
        parser.add_argument(
            "--include-running",
            action="store_true",
            help="Include scrapes with no terminal transition (label=None)",
        )

    def handle(self, *args, **opts):
        cutoff = _parse_since(opts["since"])
        self.stderr.write(f"Dumping transitions since {cutoff.isoformat()}")

        # Build terminal-outcome lookup once.
        terminal_by_scrape: dict[int, str] = {}
        for row in (
            ScrapeStatus.objects
            .filter(
                graph_node__in=TERMINAL_SUCCESS | TERMINAL_FAILURE,
                created_at__gte=cutoff,
            )
            .order_by("scrape_id", "-created_at")
            .values("scrape_id", "graph_node")
        ):
            terminal_by_scrape.setdefault(row["scrape_id"], row["graph_node"])

        out = sys.stdout
        emitted = 0
        for row in (
            ScrapeStatus.objects
            .filter(graph_node__isnull=False, created_at__gte=cutoff)
            .order_by("scrape_id", "created_at", "id")
            .values("scrape_id", "graph_node", "graph_payload", "note", "created_at")
        ):
            terminal = terminal_by_scrape.get(row["scrape_id"])
            if not terminal and not opts["include_running"]:
                continue
            label = None
            if terminal in TERMINAL_SUCCESS:
                label = "success"
            elif terminal in TERMINAL_FAILURE:
                label = "failure"

            record = {
                "scrape_id": row["scrape_id"],
                "graph_node": row["graph_node"],
                "graph_payload": row["graph_payload"],
                "note": row["note"],
                "created_at": (
                    row["created_at"].isoformat() if row["created_at"] else None
                ),
                "terminal_node": terminal,
                "label": label,
            }
            out.write(json.dumps(record, default=str))
            out.write("\n")
            emitted += 1

        total_scrapes = len(terminal_by_scrape)
        self.stderr.write(
            f"Emitted {emitted} transitions from {total_scrapes} terminated scrapes."
        )


def _parse_since(raw: str) -> datetime:
    raw = (raw or "").strip()
    if raw.endswith("d") and raw[:-1].isdigit():
        return timezone.now() - timedelta(days=int(raw[:-1]))
    if raw.endswith("h") and raw[:-1].isdigit():
        return timezone.now() - timedelta(hours=int(raw[:-1]))
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_timezone.utc)
        return dt
    except Exception:
        return timezone.now() - timedelta(days=7)
