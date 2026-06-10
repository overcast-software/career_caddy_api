"""Diagnostic surface for scrapes that fail post-extract.

Background: from-text scrapes (paste-to-JobPost path) silently swallow
post-extract failures. When the extractor returns placeholder
``title`` / ``company_name`` or raises, ``parse_scrape`` flips
``scrape.status = "failed"`` and logs an ``exception`` line into the
app logs — but the operator only sees "could not parse" in the
extension popup or a vague "failed" badge in the scrapes list. The
underlying ``logger.exception`` traceback is in stdout of the api
container; the user has no diagnostic surface to act on.

Fix: persist a short human-readable summary on the Scrape row at
each ``status=failed`` write site. The frontend can render it
verbatim (jp.show, scrapes index, extension popup) without parsing
the rolling app log.

Schema: ``failure_reason TextField(null=True, blank=True, max_length=2000)``.
NULL for non-failed rows. Backfill is implicit — existing failed
rows stay NULL; only new failures populate.

Reversibility: the field drop reverses cleanly. No data migration
needed.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0101_tighten_linkedin_extraction_hints"),
    ]

    operations = [
        migrations.AddField(
            model_name="scrape",
            name="failure_reason",
            field=models.TextField(
                null=True, blank=True, max_length=2000
            ),
        ),
    ]
