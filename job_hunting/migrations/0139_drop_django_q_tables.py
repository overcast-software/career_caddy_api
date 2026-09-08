"""CC-208 — drop the django-q2 tables. The exit-gate on epic CC-200.

The qcluster bridge worker (CC-199) is gone: the `worker` Cloud Run service
was destroyed on 2026-09-03 and `gcloud run services list --region=us-west1`
shows six services with no `worker` among them (re-verified 2026-09-07).
This migration removes the last thing it left behind.

WHAT THIS DROPS
    django_q_ormq      the queue table — the actual broker
    django_q_schedule  recurring Schedule rows (superseded by SCHEDULE_REGISTRY)
    django_q_task      historical task results

PRE-FLIGHT: `django_q_ormq` MUST BE EMPTY
    A row in django_q_ormq is a job that was enqueued and never drained.
    Dropping the table with rows in it loses that work SILENTLY, so the first
    operation below counts them and logs at ERROR if any exist. It does not
    refuse — by the time this runs there is no worker to drain them either, so
    blocking the migration would only wedge the deploy without saving the row.
    Check before you apply:

        SELECT count(*) FROM django_q_ormq;

    Verified 0 on the dev database 2026-09-07. Only `enqueue_cover_letter`'s
    fallback still wrote here after CC-207b, and this same change removes it.

WHY THERE ARE NO state_operations
    api/CLAUDE.md requires RunSQL that touches a column, index or constraint to
    carry matching `state_operations`, because `makemigrations` compares
    migration STATE to the model classes and never looks at a database — raw
    SQL can otherwise desync the two (0124's PK swap did exactly that).

    That rule does not apply here, and the reason is worth stating rather than
    leaving as an omission: these tables belong to `django_q`, a THIRD-PARTY
    APP being removed from INSTALLED_APPS in this same change. Once it is
    uninstalled it contributes no nodes and no models to the migration graph,
    so there is no state describing these tables for a `state_operations` block
    to keep in sync. `job_hunting`'s own state is untouched.

FRESH DATABASES
    With django_q out of INSTALLED_APPS a new database never creates these
    tables at all, so every statement here is written `IF EXISTS` and this
    migration is a legitimate no-op on a clean install.

IRREVERSIBLE IN PRACTICE
    The reverse is a no-op rather than a raise. Re-creating the tables would
    need django-q2's own migrations, and the app is gone — but the six
    schedule-registering migrations this supersedes (0086, 0090, 0109, 0111,
    0113, 0127) were neutralised to empty in the same change, so the graph
    still unwinds cleanly past this point. Rolling back restores no data.
"""

import logging

from django.db import migrations

logger = logging.getLogger(__name__)

DJANGO_Q_TABLES = (
    "django_q_ormq",
    "django_q_schedule",
    "django_q_task",
)


def report_pending_work(apps, schema_editor):
    """Log what is about to be destroyed. Never silently drop queued jobs."""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('public.django_q_ormq') IS NOT NULL")
        (exists,) = cursor.fetchone()
        if not exists:
            logger.info("cc208: django_q_ormq absent; nothing to drop")
            return

        cursor.execute("SELECT count(*) FROM django_q_ormq")
        (pending,) = cursor.fetchone()

    if pending:
        # ERROR, not WARNING: this is unrecoverable work loss and it should be
        # impossible to miss in a deploy log.
        logger.error(
            "cc208: DROPPING django_q_ormq WITH %s UNDRAINED JOB(S). "
            "No qcluster worker exists to drain them, so these are lost.",
            pending,
        )
    else:
        logger.info("cc208: django_q_ormq is empty; safe to drop")


def forget_django_q_migrations(apps, schema_editor):
    """Remove django_q's rows from django_migrations.

    Cosmetic but deliberate: leaving them describes an app that is no longer
    installed, which makes `showmigrations` report a phantom and misleads the
    next reader into thinking django-q2 is still part of the stack.
    """
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("DELETE FROM django_migrations WHERE app = 'django_q'")
        logger.info("cc208: removed %s django_q migration record(s)", cursor.rowcount)


DROP_SQL = "\n".join(f"DROP TABLE IF EXISTS {t} CASCADE;" for t in DJANGO_Q_TABLES)


class Migration(migrations.Migration):
    dependencies = [
        ("job_hunting", "0138_capture_model_state_drift"),
    ]

    operations = [
        migrations.RunPython(report_pending_work, migrations.RunPython.noop),
        migrations.RunSQL(DROP_SQL, reverse_sql=migrations.RunSQL.noop),
        migrations.RunPython(forget_django_q_migrations, migrations.RunPython.noop),
    ]
