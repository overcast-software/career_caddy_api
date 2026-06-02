"""Phase 5d outbound dispatch — register the dispatch-sweep schedule.

Belt-and-suspenders for cases where ``dispatch_one`` was scheduled via
``async_task(... schedule=<future>)`` but the qcluster worker was down
when the time came (or crashed mid-attempt). The sweep runs once a
minute, picks up any ``FederationActivity`` outbound row with
``delivery_status='pending' AND next_attempt_at <= now()``, and
re-enqueues a fresh ``dispatch_one`` task.

Mirrors the pattern from 0086_register_scrape_claim_sweep_schedule —
``update_or_create`` keyed on schedule name so re-runs refresh in place
instead of stacking. Reverse drops the row.
"""
from django.db import migrations


SCHEDULE_NAME = "federation_dispatch_sweep"
SCHEDULE_FUNC = "job_hunting.lib.federation_dispatch.sweep_pending_dispatches"
SCHEDULE_INTERVAL_MINUTES = 1


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
        ("job_hunting", "0089_federation_dispatch_fields"),
        ("django_q", "0019_alter_task_options_alter_ormq_key_alter_ormq_lock_and_more"),
    ]

    operations = [
        migrations.RunPython(register_schedule, reverse_code=drop_schedule),
    ]
