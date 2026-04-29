"""Backfill JobPostDiscovery rows for user_id=2 (dough).

JobPost is a shared resource; per-user visibility flows through the new
JobPostDiscovery join. Existing rows pre-date the join, so without this
backfill dough's frontend list view (which scopes by discovery) would
hide every historical post until it was re-ingested.

Idempotent: skips rows that already exist; no-ops if user 2 is absent
(fresh installs / CI).
"""

from django.db import migrations


TARGET_USER_ID = 2


def backfill(apps, schema_editor):
    User = apps.get_model("auth", "User")
    JobPost = apps.get_model("job_hunting", "JobPost")
    JobPostDiscovery = apps.get_model("job_hunting", "JobPostDiscovery")

    if not User.objects.filter(id=TARGET_USER_ID).exists():
        return

    existing = set(
        JobPostDiscovery.objects.filter(user_id=TARGET_USER_ID)
        .values_list("job_post_id", flat=True)
    )
    rows = [
        JobPostDiscovery(
            user_id=TARGET_USER_ID,
            job_post_id=jp.id,
            source=jp.source or "manual",
        )
        for jp in JobPost.objects.only("id", "source").iterator()
        if jp.id not in existing
    ]
    if rows:
        JobPostDiscovery.objects.bulk_create(rows, batch_size=1000)


def reverse(apps, schema_editor):
    JobPostDiscovery = apps.get_model("job_hunting", "JobPostDiscovery")
    JobPostDiscovery.objects.filter(user_id=TARGET_USER_ID).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0064_job_post_discovery"),
    ]

    operations = [
        migrations.RunPython(backfill, reverse),
    ]
