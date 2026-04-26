from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('job_hunting', '0055_jobpost_dedupe_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='scrapeprofile',
            name='apply_resolver_config',
            field=models.JSONField(blank=True, null=True),
        ),
    ]
