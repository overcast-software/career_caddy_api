from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0025_job_post_salary_location_remote"),
    ]

    operations = [
        migrations.AddField(
            model_name="coverletter",
            name="status",
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
        migrations.AddField(
            model_name="answer",
            name="status",
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
    ]
