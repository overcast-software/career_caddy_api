# Generated migration to add 'updating_profile' scrape status

from django.db import migrations


def seed_status(apps, schema_editor):
    Status = apps.get_model("job_hunting", "Status")
    Status.objects.get_or_create(
        status="updating_profile",
        status_type="scrape",
    )


def remove_status(apps, schema_editor):
    Status = apps.get_model("job_hunting", "Status")
    Status.objects.filter(status="updating_profile", status_type="scrape").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0044_resume_status_field"),
    ]

    operations = [
        migrations.RunPython(seed_status, remove_status),
    ]
