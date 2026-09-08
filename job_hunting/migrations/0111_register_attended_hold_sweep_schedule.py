"""Register the orphaned-attended-hold sweep (CC-32).

NEUTRALISED BY CC-208 (2026-09-07) — this migration is now a NO-OP.

WHAT IT ORIGINALLY DID
    Created a django-q2 Schedule row for sweep_orphaned_attended_holds — a worker fn that was NEVER implemented (CC-219). 0127 deleted the row again; both are now no-ops.

WHY IT IS EMPTY NOW
    django-q2 was removed from INSTALLED_APPS when the qcluster bridge
    worker was retired (CC-208, the exit-gate on CC-200). This file used to
    carry two couplings to that app, and BOTH break a fresh `migrate` once
    it is gone:

      * a graph dependency on ("django_q", "0019_alter_task_options_...")
        -> NodeNotFoundError, because the app no longer contributes nodes;
      * apps.get_model("django_q", "Schedule") inside RunPython
        -> LookupError.

    Editing an applied migration is normally wrong. It is the correct remedy
    HERE because the operation was a DATA migration, not a schema one, so it
    contributes nothing to migration state and removing it cannot cause
    drift (`makemigrations --check` compares state to models and never sees
    RunPython). Concretely:

      * on an EXISTING database this migration is already applied and its
        body is never executed again;
      * on a FRESH database the row it used to write would be meaningless —
        nothing reads django_q_schedule any more.

    The schedule this registered is NOT lost. Recurring sweeps moved to
    lib/schedule_kinds.py SCHEDULE_REGISTRY, driven by Cloud Scheduler ->
    /tasks/run-scheduled/ on GCP and by the `run_jobs` loop on self-host
    (CC-213). That is the live mechanism; this row had been a duplicate
    second driver of the same sweep for as long as both existed.

    The django_q_* tables themselves are dropped by
    0139_drop_django_q_tables.
"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("job_hunting", "0110_scrape_attended"),
    ]

    # Deliberately empty. See the module docstring.
    operations = []
