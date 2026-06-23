"""Bounded retention prune for Scrape.html (PACA #30).

Keeps the most-recent N *completed* scrapes per host with their captured
DOM and nulls html on older completed rows, so successful captures stay
inspectable (scrape-profile-enhancer, readiness live-match) without raw
html growing unbounded. Failed / non-terminal rows are never touched.

Operator entrypoint for the same logic the django-q2 schedule runs
hourly (job_hunting.lib.tasks.prune_scrape_html, registered by
migration 0109).

Usage::

    python manage.py prune_scrape_html
    python manage.py prune_scrape_html --keep-per-host=3
    python manage.py prune_scrape_html --dry-run
"""
from django.core.management.base import BaseCommand

from job_hunting.lib.tasks import prune_scrape_html


class Command(BaseCommand):
    help = "Keep most-recent N completed scrapes' html per host; null older."

    def add_arguments(self, parser):
        parser.add_argument("--keep-per-host", type=int, default=1)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, keep_per_host, dry_run, **opts):
        result = prune_scrape_html(keep_per_host=keep_per_host, dry_run=dry_run)
        verb = "would null" if dry_run else "nulled"
        n = result["would_null"] if dry_run else result["nulled"]
        self.stdout.write(
            f"{verb} {n} scrape html blobs "
            f"(kept {result['kept']} across {result['hosts']} hosts, "
            f"keep_per_host={result['keep_per_host']})"
        )
