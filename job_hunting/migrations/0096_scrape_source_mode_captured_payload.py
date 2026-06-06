"""Phase A of the Extension direct-POST plan.

Adds two fields to ``scrape``:

* ``source_mode`` — char(32), default ``browser``, choices
  ``[(browser, browser), (extension-direct, extension-direct)]``.
  Indexed so the future Phase D operator panel can count fast-path
  scrapes per day cheaply. Existing rows backfill to ``browser`` via
  the default — the historical Camoufox/Playwright capture path.
* ``captured_payload`` — JSONField nullable, no default. Holds the
  extension-side title / company / description / apply_url / location /
  extraction_hints when ``source_mode='extension-direct'``. NULL on
  every browser-mode row.

Backfill story: AddField with a default fills every existing row with
``source_mode='browser'`` synchronously inside the migration. Table is
small (<100k rows in prod) so the in-place rewrite is acceptable —
mirrors the 0092 extension_prefill migration pattern.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0095_jpd_requested_by"),
    ]

    operations = [
        migrations.AddField(
            model_name="scrape",
            name="source_mode",
            field=models.CharField(
                choices=[
                    ("browser", "browser"),
                    ("extension-direct", "extension-direct"),
                ],
                db_index=True,
                default="browser",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="scrape",
            name="captured_payload",
            field=models.JSONField(blank=True, default=None, null=True),
        ),
    ]
