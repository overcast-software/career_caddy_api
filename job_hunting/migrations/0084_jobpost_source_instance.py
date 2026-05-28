"""Add JobPost.source_instance for ActivityPub federation origin tracking.

Single-instance installs see no behavioral change: every existing row
gets the local `CAREER_CADDY_INSTANCE` as its source_instance, and new
rows get the same default via the model callable. The column matters
once a second Career Caddy instance enters the picture — then the
five-clause visibility filter consults this column to exclude federated
rows from default views unless the user has subscribed to that instance.

Hand-written rather than makemigrations-generated so:
- The backfill default is computed at runtime via the model callable
  rather than baked into the migration as a string literal (which would
  freeze prod's hostname into history).
- The dependency chain is explicit: must run after 0083 (audience),
  since both fields ship as the Phase 4 ActivityPub readiness pair.
"""

import job_hunting.models.job_post
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0083_jobpost_audience"),
    ]

    operations = [
        migrations.AddField(
            model_name="jobpost",
            name="source_instance",
            field=models.CharField(
                db_index=True,
                default=job_hunting.models.job_post._default_source_instance,
                max_length=255,
            ),
        ),
    ]
