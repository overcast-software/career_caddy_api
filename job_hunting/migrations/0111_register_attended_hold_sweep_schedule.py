"""PACA CC #32: register the orphaned-attended-hold staleness sweep.

Creates a django-q2 Schedule row that fires
``job_hunting.lib.tasks.sweep_orphaned_attended_holds`` every 5 minutes.
The qcluster process picks the Schedule row up automatically — no extra
deploy step. With no args the task reads ``CC_ATTENDED_HOLD_*`` from
settings: the observability leg always runs, and the auto-demote leg
stays OFF until ``CC_ATTENDED_HOLD_TTL_MINUTES > 0`` (default-safe).

Idempotent: update_or_create keyed on the schedule name, so re-running
the migration just refreshes the row instead of stacking schedules.
Reverse drops the row. Mirrors 0086_register_scrape_claim_sweep_schedule
and 0109_scrape_html_prune_schedule.
"""
from django.db import migrations


SCHEDULE_NAME = "sweep_orphaned_attended_holds"
SCHEDULE_FUNC = "job_hunting.lib.tasks.sweep_orphaned_attended_holds"
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
        ("job_hunting", "0110_scrape_attended"),
        # Depend on django_q's schema being in place before we INSERT a row.
        ("django_q", "0019_alter_task_options_alter_ormq_key_alter_ormq_lock_and_more"),
    ]

    operations = [
        migrations.RunPython(register_schedule, reverse_code=drop_schedule),
    ]
