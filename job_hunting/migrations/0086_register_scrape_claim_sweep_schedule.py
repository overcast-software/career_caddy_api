"""Phase 2 of Plans/Scrape runner: register the lease-sweep schedule.

Creates a django-q2 Schedule row that fires
``job_hunting.lib.tasks.sweep_stale_scrape_claims`` every 5 minutes. The
qcluster process picks the Schedule row up automatically — no extra
deploy step.

Idempotent: uses update_or_create keyed on the schedule name, so re-
running the migration (or rolling forward a duplicate definition) just
refreshes the row instead of stacking schedules. Reverse drops the row.
"""
from django.db import migrations


SCHEDULE_NAME = "sweep_stale_scrape_claims"
SCHEDULE_FUNC = "job_hunting.lib.tasks.sweep_stale_scrape_claims"
SCHEDULE_INTERVAL_MINUTES = 5


def register_schedule(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.update_or_create(
        name=SCHEDULE_NAME,
        defaults={
            "func": SCHEDULE_FUNC,
            # 'I' = MINUTES interval. See django_q.models.Schedule.MINUTES.
            "schedule_type": "I",
            "minutes": SCHEDULE_INTERVAL_MINUTES,
            "repeats": -1,  # forever
        },
    )


def drop_schedule(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.filter(name=SCHEDULE_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("job_hunting", "0085_scrape_claimed_at"),
        # Depend on django_q's schema being in place before we INSERT a row.
        # 0019 is the current head (django-q2 1.10.0); pinning the latest
        # available migration ensures the Schedule table exists.
        ("django_q", "0019_alter_task_options_alter_ormq_key_alter_ormq_lock_and_more"),
    ]

    operations = [
        migrations.RunPython(register_schedule, reverse_code=drop_schedule),
    ]
