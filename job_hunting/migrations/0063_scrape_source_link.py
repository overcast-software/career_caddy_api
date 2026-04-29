from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0062_seed_apply_resolver_configs"),
    ]

    operations = [
        migrations.AddField(
            model_name="scrape",
            name="source_link",
            field=models.CharField(max_length=2000, null=True, blank=True),
        ),
    ]
