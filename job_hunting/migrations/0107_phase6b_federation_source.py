"""Phase 6b — federation source values.

Two schema-adjacent changes:

1. Expand ``Company.source`` choices to include ``"federation"`` so the
   inbound Phase 6b ingest pipeline can mint a Company on
   ``careercaddy:extension.company`` lookup-miss with provenance that
   doesn't masquerade as an LLM extraction.
2. Data-migrate any existing ``JobPost.source = "activitypub"`` rows
   (the Phase 5e source value) to ``"federation"`` — Phase 6b
   standardises the source label across the federated write path.
   ``JobPost.source`` is a free-form CharField (no DB ``choices``
   constraint), so the rename is data-only; no schema migration.
"""
from __future__ import annotations

from django.db import migrations, models


def forwards(apps, schema_editor):
    JobPost = apps.get_model("job_hunting", "JobPost")
    JobPost.objects.filter(source="activitypub").update(source="federation")


def backwards(apps, schema_editor):
    JobPost = apps.get_model("job_hunting", "JobPost")
    JobPost.objects.filter(source="federation").update(source="activitypub")


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0106_phase6a_company_actors"),
    ]

    operations = [
        migrations.AlterField(
            model_name="company",
            name="source",
            field=models.CharField(
                blank=True,
                choices=[
                    ("extraction", "extraction"),
                    ("manual", "manual"),
                    ("backfill", "backfill"),
                    ("federation", "federation"),
                ],
                max_length=32,
                null=True,
            ),
        ),
        migrations.RunPython(forwards, backwards),
    ]
