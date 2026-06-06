from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0091_federation_ingest_dupe_action"),
    ]

    operations = [
        migrations.AddField(
            model_name="scrape",
            name="extension_prefill",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
