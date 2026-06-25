"""Add Scrape.created_at — the FIFO key for claim-next (CC-77).

The Scrape PK is now a random NanoID (0122), so the old integer-autoinc
id can no longer serve as the arrival-order key the claim-next queue
relied on. Add a nullable ``created_at`` (``auto_now_add``) so new rows
carry a real creation timestamp; the column is nullable purely so this
additive migration needs no backfill on existing rows (which sort first
as the oldest holds).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("job_hunting", "0124_company_nanoid_pk_swap"),
    ]

    operations = [
        migrations.AddField(
            model_name="scrape",
            name="created_at",
            field=models.DateTimeField(auto_now_add=True, null=True),
        ),
    ]
