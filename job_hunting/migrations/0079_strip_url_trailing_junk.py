"""One-off scrub for JobPost.link / apply_url / canonical_link rows whose
trailing character is HTML/markdown delimiter slop (typically a stray ``"``
captured by the LLM URL extractor in cc_auto). The 2026-05-27 hiring.cafe
JP 2981 incident is the canonical case: link stored as
``https://hiring.cafe/job/5fsbbgitg82ev1ar"``, frontend URL-encoded the ``"``
to ``%22`` in the href, hiring.cafe 404'd.

Per-row save() now sanitizes via ``strip_url_trailing_junk`` so new writes
can't reproduce this; this migration backfills the long tail.

Idempotent: only rewrites when stripping changes the value.
"""

from django.db import migrations


_TRAILING_JUNK_CHARS = "\"'<>()[]{}`, \t\n\r"


def _strip(value):
    if not value:
        return value
    stripped = value.rstrip(_TRAILING_JUNK_CHARS)
    return stripped if stripped else value


def scrub(apps, schema_editor):
    JobPost = apps.get_model("job_hunting", "JobPost")
    fields = ("link", "apply_url", "canonical_link")
    qs = JobPost.objects.all().only("id", *fields)
    to_update = []
    for jp in qs.iterator():
        updates = {}
        for field in fields:
            current = getattr(jp, field, None)
            cleaned = _strip(current)
            if cleaned != current:
                updates[field] = cleaned
        if not updates:
            continue
        for field, value in updates.items():
            setattr(jp, field, value)
        to_update.append(jp)
        if len(to_update) >= 500:
            JobPost.objects.bulk_update(to_update, list(fields))
            to_update.clear()
    if to_update:
        JobPost.objects.bulk_update(to_update, list(fields))


def reverse(apps, schema_editor):
    # No-op: the original junk-suffix values are unrecoverable and not
    # worth recreating.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0078_drop_scrape_apply_url_from_hint"),
    ]

    operations = [
        migrations.RunPython(scrub, reverse),
    ]
