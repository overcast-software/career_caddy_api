"""BACK-91 — decouple ingestion from publishing.

Two schema changes, no data migration (future behavior only):

1. ``JobPost.audience`` default flips from ``[AS2_PUBLIC]`` (public) to
   ``[]`` (private). Ingesting a job into your own library is no longer a
   publishing act. AlterField only changes the default for FUTURE inserts —
   existing rows (including the ~3.4K legacy ``audience=NULL`` rows) are
   left untouched, which is the desired private end-state.
2. Add ``Profile.federate_posts`` (default ``False``) — the per-user
   publish opt-in. Existing Profile rows backfill to ``False`` (opted out),
   matching the new private-by-default principle. When True, the ingestion
   paths mark freshly-created posts public via ``JobPost.audience_for_user``.

There is intentionally NO RunPython backfill: flipping legacy rows to public
would publish them, which is exactly what BACK-91 forbids.
"""
from __future__ import annotations

import job_hunting.models.job_post
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0111_register_attended_hold_sweep_schedule"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="federate_posts",
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name="jobpost",
            name="audience",
            field=models.JSONField(
                blank=True,
                default=job_hunting.models.job_post._default_audience_private,
            ),
        ),
    ]
