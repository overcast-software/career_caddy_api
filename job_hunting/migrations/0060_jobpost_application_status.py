from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0059_seed_linkedin_rememberme_candidates"),
    ]

    operations = [
        migrations.AddField(
            model_name="jobpost",
            name="application_status",
            field=models.CharField(
                blank=True,
                choices=[("open", "Open"), ("closed", "Closed")],
                db_index=True,
                max_length=16,
                null=True,
            ),
        ),
    ]
