"""Operator-invoked backfill of JobPost.canonical_link + apply_url.

When canonicalize_link logic changes (new tracking-param entry, new
ScrapeProfile.url_rewrites rule, etc.), legacy rows keep their old
canonical_link / apply_url until something rewrites them. This command
iterates every JobPost and re-applies the live canonicalizers,
bulk-updating any rows whose result changed.

`apply_url` gets the same treatment (CC-139): tracking params / personal
tokens baked into an apply destination break the filter[link] popup
lookup's exact-equality legs, and the value is only canonicalized at
write going forward — this pass rewrites the historical rows.

Run after deploy + any MCP-driven ScrapeProfile rule edits that should
apply retroactively.
"""

from django.core.management.base import BaseCommand

from job_hunting.models import JobPost
from job_hunting.models.job_post_dedupe import (
    _profile_url_rewrites_for_host,
    canonicalize_apply_url,
    canonicalize_link,
)


class Command(BaseCommand):
    help = (
        "Re-apply canonicalize_link to every JobPost.link and "
        "canonicalize_apply_url to every JobPost.apply_url; bulk-update changes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch", type=int, default=500,
            help="bulk_update batch size (default: 500)",
        )

    def handle(self, *args, batch, **options):
        _profile_url_rewrites_for_host.cache_clear()
        seen = link_changed = apply_changed = 0
        pending = []
        qs = JobPost.objects.only("id", "link", "canonical_link", "apply_url")
        for jp in qs.iterator():
            seen += 1
            dirty_fields = set()
            if jp.link:
                new_canonical = canonicalize_link(jp.link)
                if new_canonical != jp.canonical_link:
                    jp.canonical_link = new_canonical
                    dirty_fields.add("canonical_link")
                    link_changed += 1
            if jp.apply_url:
                new_apply = canonicalize_apply_url(jp.apply_url)
                if new_apply != jp.apply_url:
                    jp.apply_url = new_apply
                    dirty_fields.add("apply_url")
                    apply_changed += 1
            if dirty_fields:
                pending.append((jp, dirty_fields))
            if len(pending) >= batch:
                self._flush(pending)
                pending = []
        if pending:
            self._flush(pending)
        self.stdout.write(self.style.SUCCESS(
            f"recanonicalize: {seen} scanned, "
            f"{link_changed} canonical_link updated, "
            f"{apply_changed} apply_url updated"
        ))

    @staticmethod
    def _flush(pending):
        """Bulk-update a batch, writing only the fields that changed.

        A row may have a dirty canonical_link, apply_url, or both; group
        by the union of dirty fields so bulk_update touches only what
        moved (a single UPDATE with the union column set is fine — an
        unchanged column just re-writes its current value).
        """
        fields = set()
        for _jp, dirty in pending:
            fields |= dirty
        JobPost.objects.bulk_update([jp for jp, _d in pending], sorted(fields))
