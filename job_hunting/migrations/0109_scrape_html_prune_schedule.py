"""PACA #30: register the Scrape.html retention prune schedule.

Creates a django-q2 Schedule row that fires
``job_hunting.lib.tasks.prune_scrape_html`` hourly. The qcluster process
picks the Schedule row up automatically — no extra deploy step. With no
args the task uses keep_per_host=1, so each host keeps only its
most-recent completed scrape's captured DOM.

Idempotent: update_or_create keyed on the schedule name, so re-running
the migration just refreshes the row instead of stacking schedules.
Reverse drops the row. Mirrors 0086_register_scrape_claim_sweep_schedule.
"""
from django.db import migrations


SCHEDULE_NAME = "prune_scrape_html"
SCHEDULE_FUNC = "job_hunting.lib.tasks.prune_scrape_html"
SCHEDULE_INTERVAL_MINUTES = 60


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
        ("job_hunting", "0108_phase6b_company_followers"),
        # Depend on django_q's schema being in place before we INSERT a row.
        ("django_q", "0019_alter_task_options_alter_ormq_key_alter_ormq_lock_and_more"),
    ]

    operations = [
        migrations.RunPython(register_schedule, reverse_code=drop_schedule),
    ]
