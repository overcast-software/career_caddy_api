from django.db import migrations

import job_hunting.models.profile


class Migration(migrations.Migration):

    dependencies = [
        ('job_hunting', '0047_scrapeprofile_failure_count_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='profile',
            name='onboarding',
            field=job_hunting.models.profile.SafeJSONField(
                blank=True, default=dict, null=True,
            ),
        ),
    ]
