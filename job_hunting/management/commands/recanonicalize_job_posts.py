"""Operator-invoked backfill of JobPost.canonical_link.

When canonicalize_link logic changes (new tracking-param entry, new
ScrapeProfile.url_rewrites rule, etc.), legacy rows keep their old
canonical_link until something rewrites them. This command iterates every
JobPost and re-applies the live canonicalize_link, bulk-updating any rows
whose result changed.

Run after deploy + any MCP-driven ScrapeProfile rule edits that should
apply retroactively.
"""

from django.core.management.base import BaseCommand

from job_hunting.models import JobPost
from job_hunting.models.job_post_dedupe import (
    _profile_url_rewrites_for_host,
    canonicalize_link,
)


class Command(BaseCommand):
    help = "Re-apply canonicalize_link to every JobPost.link; bulk-update changes."

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch", type=int, default=500,
            help="bulk_update batch size (default: 500)",
        )

    def handle(self, *args, batch, **options):
        _profile_url_rewrites_for_host.cache_clear()
        seen = changed = 0
        pending = []
        for jp in JobPost.objects.only("id", "link", "canonical_link").iterator():
            seen += 1
            if not jp.link:
                continue
            new_canonical = canonicalize_link(jp.link)
            if new_canonical != jp.canonical_link:
                jp.canonical_link = new_canonical
                pending.append(jp)
                changed += 1
            if len(pending) >= batch:
                JobPost.objects.bulk_update(pending, ["canonical_link"])
                pending = []
        if pending:
            JobPost.objects.bulk_update(pending, ["canonical_link"])
        self.stdout.write(self.style.SUCCESS(
            f"recanonicalize: {seen} scanned, {changed} updated"
        ))
