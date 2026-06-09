"""Phase B of the dedupe redesign — title-side normalization.

Adds ``JobPost.normalized_fingerprint``, the title+location sibling of
the existing ``content_fingerprint`` column. Where Phase A
(``0098_company_alias_and_suggestions``) collapsed company-name drift
("Allstate Corporation" vs "Allstate Insurance Company") via
``CompanyAlias.name_slug``, Phase B collapses TITLE drift —
specifically the punctuation noise that the current case+whitespace
fold in ``fingerprint`` cannot see.

Canonical regression: JP 1329 vs JP 3323. Same role
("Software Engineer - Product Security"), same company, same location;
one title carried a U+002D hyphen-minus, the other a U+2013 en-dash.
``fingerprint`` produced different sha1s because the en-dash survives
``lower()``. ``normalized_fingerprint`` passes the title through the
Phase A ``slug`` helper (NFKC fold + unicode-dash/quote → ASCII +
strip non-alnum-except-hyphen + collapse runs), so both titles
collapse to the same hash.

Two steps:

1. AddField — ``normalized_fingerprint`` (CharField, max_length=40,
   null=True, db_index=True). Null by default because the backfill
   below fills it post-schema-change; new rows populate it in
   ``JobPost.save`` via ``normalized_fingerprint(self)``.
2. Chunked RunPython backfill — iterate every JobPost, compute the
   normalized hash via the inline slug helpers, write back via a
   batched ``bulk_update`` of 500 rows at a time. The slug helpers
   are inlined (NOT imported from ``job_hunting.lib.slug``) because
   the migration framework loads migrations before the app graph is
   fully wired — same pattern Phase A used in the alias backfill.

``atomic = False`` — the chunked backfill commits each batch
independently so a transient failure mid-table doesn't roll back the
whole run. Phase A's migration set the precedent with the same
reasoning (CREATE EXTENSION + backfill); applying it here keeps the
schema-change visible quickly so other deploys aren't blocked while
the long backfill runs.
"""

from django.db import migrations, models


def backfill_normalized_fingerprint(apps, schema_editor):
    """Populate ``normalized_fingerprint`` for every existing JobPost.

    Mirrors the inline-slug-helpers pattern from
    ``0098_company_alias_and_suggestions.backfill_company_aliases`` —
    cannot import from ``job_hunting.lib.slug`` at migration-load
    time so the helpers are duplicated here. The runtime
    ``JobPost.save`` path uses the canonical helper.

    Chunked into 500-row batches via ``bulk_update`` so a single big
    transaction doesn't lock the table for the entire backfill. Order
    by pk so the iterator is deterministic across restarts.
    """
    import hashlib
    import re
    import unicodedata

    JobPost = apps.get_model("job_hunting", "JobPost")

    _DASH_TRANSLATION = {
        0x2010: "-", 0x2011: "-", 0x2012: "-", 0x2013: "-",
        0x2014: "-", 0x2015: "-", 0x2212: "-",
    }
    _QUOTE_TRANSLATION = {
        0x2018: "'", 0x2019: "'", 0x201A: "'", 0x201B: "'",
        0x201C: '"', 0x201D: '"', 0x201E: '"', 0x201F: '"',
    }
    _TRANSLATION_TABLE = {**_DASH_TRANSLATION, **_QUOTE_TRANSLATION}
    _NON_SLUG = re.compile(r"[^a-z0-9\- ]+")
    _WS = re.compile(r"\s+")
    _HYPHEN_OR_SPACE = re.compile(r"[\s-]+")

    def _slug(s):
        if not s:
            return ""
        n = unicodedata.normalize("NFKC", s).translate(_TRANSLATION_TABLE)
        n = _WS.sub(" ", n.lower()).strip()
        n = _NON_SLUG.sub("", n)
        n = _HYPHEN_OR_SPACE.sub("-", n).strip("-")
        return n

    def _normalized_fingerprint(post):
        if not (post.company_id and post.title):
            return None
        parts = [
            str(post.company_id),
            _slug(post.title),
            _slug(post.location or ""),
        ]
        return hashlib.sha1(
            "|".join(parts).encode(), usedforsecurity=False
        ).hexdigest()

    BATCH = 500
    pending = []
    qs = JobPost.objects.all().only(
        "id", "company_id", "title", "location"
    ).order_by("pk").iterator(chunk_size=BATCH)
    for post in qs:
        post.normalized_fingerprint = _normalized_fingerprint(post)
        pending.append(post)
        if len(pending) >= BATCH:
            JobPost.objects.bulk_update(pending, ["normalized_fingerprint"])
            pending = []
    if pending:
        JobPost.objects.bulk_update(pending, ["normalized_fingerprint"])


def reverse_backfill_normalized_fingerprint(apps, schema_editor):
    """Reverse pass: null out the column. RemoveField in the reverse
    schema pass drops it entirely, but if a future migration ever
    runs this without the schema reverse (data-only rewind) we want
    the column emptied first."""
    JobPost = apps.get_model("job_hunting", "JobPost")
    JobPost.objects.update(normalized_fingerprint=None)


class Migration(migrations.Migration):

    # See module docstring — each batch commits independently so a
    # transient failure mid-backfill doesn't roll back the schema
    # change. Phase A (0098) set this precedent for the same reason.
    atomic = False

    dependencies = [
        ("job_hunting", "0098_company_alias_and_suggestions"),
    ]

    operations = [
        migrations.AddField(
            model_name="jobpost",
            name="normalized_fingerprint",
            field=models.CharField(
                blank=True, db_index=True, max_length=40, null=True
            ),
        ),
        migrations.RunPython(
            backfill_normalized_fingerprint,
            reverse_backfill_normalized_fingerprint,
        ),
    ]
