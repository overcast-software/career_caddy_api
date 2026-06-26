"""BACK-98 (Task B) — per-user rich/lean federated-Note format opt-in.

Adds ``Profile.federate_rich`` (default ``False`` = LEAN). Orthogonal to
BACK-91's ``federate_posts`` (publish y/n): this selects the rendered Note
FORMAT for users who DO federate. Existing Profile rows backfill to
``False`` (lean) — the safe baseline; only an explicit opt-in (Doug's
``@dough``) gets the rich/show-off format. AddField only, no data
migration.
"""
from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0125_scrape_created_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="federate_rich",
            field=models.BooleanField(default=False),
        ),
    ]
