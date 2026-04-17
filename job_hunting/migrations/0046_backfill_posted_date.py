from django.db import migrations


def backfill_posted_date(apps, schema_editor):
    JobPost = apps.get_model("job_hunting", "JobPost")
    for jp in JobPost.objects.filter(posted_date__isnull=True):
        jp.posted_date = jp.created_at.date()
        jp.save(update_fields=["posted_date"])


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0045_seed_updating_profile_status"),
    ]

    operations = [
        migrations.RunPython(backfill_posted_date, migrations.RunPython.noop),
    ]
