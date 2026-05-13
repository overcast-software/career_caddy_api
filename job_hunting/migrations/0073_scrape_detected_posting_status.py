from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0072_scrape_skip_extract"),
    ]

    operations = [
        migrations.AddField(
            model_name="scrape",
            name="detected_posting_status",
            field=models.CharField(blank=True, max_length=16, null=True),
        ),
        migrations.AddField(
            model_name="scrape",
            name="detected_closed_evidence",
            field=models.TextField(blank=True, null=True),
        ),
    ]
