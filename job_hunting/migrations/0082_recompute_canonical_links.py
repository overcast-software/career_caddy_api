"""Recompute JobPost.canonical_link for every existing row so the
trailing-slash normalization (added in this commit) applies to history,
not just incoming writes.

The 2026-05-27 JP 715 vs JP 2963 LinkedIn pair surfaced the gap: both
posts had the same LinkedIn job id but one canonical_link ended in `/`
and the other didn't, so dedup's stage-1 exact match missed. Migration
0079_strip_url_trailing_junk only stripped HTML-delimiter slop and did
NOT recanonicalize, so historical canonical_link values still carried
whatever slash convention they were created with.

Idempotent: only rewrites when canonicalize_link returns a different
value.
"""

from django.db import migrations


def recompute(apps, schema_editor):
    JobPost = apps.get_model("job_hunting", "JobPost")
    # Import the runtime helper so the in-effect normalization rules
    # (trailing-junk strip + profile rewrites + tracking-param strip +
    # the new trailing-slash strip) are applied uniformly. Migrations
    # don't ship their own normalization logic.
    from job_hunting.models.job_post_dedupe import canonicalize_link

    qs = JobPost.objects.filter(link__isnull=False).only(
        "id", "link", "canonical_link"
    )
    batch = []
    for jp in qs.iterator():
        new_canonical = canonicalize_link(jp.link)
        if new_canonical != jp.canonical_link:
            jp.canonical_link = new_canonical
            batch.append(jp)
        if len(batch) >= 500:
            JobPost.objects.bulk_update(batch, ["canonical_link"])
            batch.clear()
    if batch:
        JobPost.objects.bulk_update(batch, ["canonical_link"])


def reverse(apps, schema_editor):
    # No-op: the pre-fix canonical_link values are unrecoverable and
    # not worth synthesizing back into a known-buggy form.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0081_backfill_duplicate_annotations"),
    ]

    operations = [
        migrations.RunPython(recompute, reverse),
    ]
