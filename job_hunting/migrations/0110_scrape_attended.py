# Attended-scrape routing — partition the hold claim queue.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0109_scrape_html_prune_schedule"),
    ]

    operations = [
        migrations.AddField(
            model_name="scrape",
            name="attended",
            field=models.BooleanField(db_index=True, default=False),
        ),
    ]
