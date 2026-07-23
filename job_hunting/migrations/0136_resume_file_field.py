# CC-204 — add Resume.file (durable uploaded-resume blob via default_storage).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0135_job"),
    ]

    operations = [
        migrations.AddField(
            model_name="resume",
            name="file",
            field=models.FileField(
                blank=True, max_length=500, null=True, upload_to="resumes/%Y/%m/"
            ),
        ),
    ]
