from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0027_summary_status"),
    ]

    operations = [
        migrations.RenameField(
            model_name="scrape",
            old_name="state",
            new_name="status",
        ),
    ]
