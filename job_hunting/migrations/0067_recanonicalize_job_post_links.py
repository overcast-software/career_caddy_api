"""Re-canonicalize JobPost.canonical_link with the host-rewrite-aware helper.

The previous canonicalize_link() only stripped tracking query params. The
new version (PR landing alongside this migration) also applies host-scoped
path rewrites from ScrapeProfile.url_rewrites — collapsing variants like
LinkedIn /comm/jobs/view/ → /jobs/view/ onto a single canonical form so
dedup recognises duplicate-form submissions of the same underlying job.

Without this backfill, legacy rows keep their old canonical_link (raw
form) and the new from-text dedup query misses them. Re-running the new
canonicalize_link over every row is cheap (no LLM, no network) and
idempotent — rows whose canonical_link already matches the new output
are written back unchanged.
"""

from django.db import migrations


def recanonicalize(apps, schema_editor):
    # Use the live helper, not a frozen historical version, so this
    # migration always reflects current canonicalization rules. The
    # helper's only model dependency is ScrapeProfile, which by this
    # migration's position is already in its current shape.
    from job_hunting.models.job_post_dedupe import canonicalize_link

    JobPost = apps.get_model("job_hunting", "JobPost")

    updates = []
    for jp in JobPost.objects.only("id", "link", "canonical_link").iterator():
        if not jp.link:
            continue
        new_canonical = canonicalize_link(jp.link)
        if new_canonical != jp.canonical_link:
            jp.canonical_link = new_canonical
            updates.append(jp)
        if len(updates) >= 500:
            JobPost.objects.bulk_update(updates, ["canonical_link"])
            updates = []
    if updates:
        JobPost.objects.bulk_update(updates, ["canonical_link"])


class Migration(migrations.Migration):
    dependencies = [
        ("job_hunting", "0066_job_post_description_decision"),
    ]

    operations = [
        migrations.RunPython(recanonicalize, reverse_code=migrations.RunPython.noop),
    ]
