"""PACA CC-114: retire the orphaned-attended-hold sweep schedule.

Attended-scrape routing is gone — a scrape is a scrape, and the
``sweep_orphaned_attended_holds`` task + its django-q2 Schedule row (created
by 0111) no longer exist. Deployed databases still carry that Schedule row,
and the qcluster would keep trying to import a task that's been deleted, so
this migration explicitly DELETES the row forward.

The reverse is a deliberate no-op: the task is gone, so there is nothing to
re-register on rollback (re-creating a Schedule that points at a missing
func would just resurrect the import error). Mirrors the drop half of
0111_register_attended_hold_sweep_schedule.
"""
from django.db import migrations


SCHEDULE_NAME = "sweep_orphaned_attended_holds"


def drop_schedule(apps, schema_editor):
    Schedule = apps.get_model("django_q", "Schedule")
    Schedule.objects.filter(name=SCHEDULE_NAME).delete()


def noop(apps, schema_editor):
    # The task no longer exists; re-registering the schedule on reverse
    # would only point at a missing func. Intentionally do nothing.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("job_hunting", "0126_profile_federate_rich"),
        # Depend on django_q's schema being in place before we touch a row.
        ("django_q", "0019_alter_task_options_alter_ormq_key_alter_ormq_lock_and_more"),
    ]

    operations = [
        migrations.RunPython(drop_schedule, reverse_code=noop),
    ]
