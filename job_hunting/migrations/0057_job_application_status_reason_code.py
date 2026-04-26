from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0055_jobpost_dedupe_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="jobapplicationstatus",
            name="reason_code",
            field=models.CharField(
                blank=True,
                choices=[
                    ("compensation", "Compensation"),
                    ("location", "Location / remote"),
                    ("seniority", "Seniority mismatch"),
                    ("stack", "Tech / stack mismatch"),
                    ("company", "Dislike company"),
                    ("other", "Other"),
                ],
                db_index=True,
                max_length=32,
                null=True,
            ),
        ),
    ]
