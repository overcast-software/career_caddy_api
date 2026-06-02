# Generated for ActivityPub Phase 5e — federated JobPost ingestion.
#
# Extends DuplicateAnnotation.action choices with ``federated_merge`` so
# the 5e ingest decision tree can audit every inbound Create(Note) that
# merges into an existing local JobPost. choices= is Django-side only
# (CharField has no DB check constraint), so the schema change is
# metadata-only; no SQL runs on apply.
#
# Other model drift (auto-pk BigAutoField widening, index name
# autorenames) Django wants to capture against this codebase is
# intentionally NOT included here — those belong on a separate
# housekeeping migration not coupled to 5e.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0090_federation_dispatch_sweep_schedule"),
    ]

    operations = [
        migrations.AlterField(
            model_name="duplicateannotation",
            name="action",
            field=models.CharField(
                choices=[
                    ("mark", "mark"),
                    ("unlink", "unlink"),
                    ("promote", "promote"),
                    ("historical", "historical"),
                    ("federated_merge", "federated_merge"),
                ],
                max_length=16,
            ),
        ),
    ]
