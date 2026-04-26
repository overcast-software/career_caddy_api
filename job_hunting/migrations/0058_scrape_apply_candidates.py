from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('job_hunting', '0057_job_application_status_reason_code'),
    ]

    operations = [
        migrations.AddField(
            model_name='scrape',
            name='apply_candidates',
            field=models.JSONField(blank=True, null=True),
        ),
    ]
