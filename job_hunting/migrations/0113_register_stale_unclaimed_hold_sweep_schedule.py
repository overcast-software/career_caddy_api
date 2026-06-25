"""PACA CC-74: register the unclaimed-hold staleness observability sweep.

Creates a django-q2 Schedule row that fires
``job_hunting.lib.tasks.sweep_stale_unclaimed_holds`` every 5 minutes.
The qcluster process picks the Schedule row up automatically — no extra
deploy step. With no args the task uses ``_DEFAULT_HOLD_STALE_MINUTES``
(30 min) as the staleness threshold.

The task is read-only — it logs a per-partition WARNING
(``scrape.holds.stale count=N oldest_age_min=M attended=<bool>``) so a
dead/absent scrape runner stops being invisible. It never mutates rows;
the attended=True partition's TTL auto-demote/fail action stays owned by
``sweep_orphaned_attended_holds`` (registered by 0111).

Idempotent: update_or_create keyed on the schedule name, so re-running
the migration just refreshes the row instead of stacking schedules.
Reverse drops the row. Mirrors 0111_register_attended_hold_sweep_schedule
and 0109_scrape_html_prune_schedule.
"""
from django.db import migrations


SCHEDULE_NAME = "sweep_stale_unclaimed_holds"
SCHEDULE_FUNC = "job_hunting.lib.tasks.sweep_stale_unclaimed_holds"
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
        ("job_hunting", "0112_jobpost_private_audience_and_federate_optin"),
        # Depend on django_q's schema being in place before we INSERT a row.
        ("django_q", "0019_alter_task_options_alter_ormq_key_alter_ormq_lock_and_more"),
    ]

    operations = [
        migrations.RunPython(register_schedule, reverse_code=drop_schedule),
    ]
