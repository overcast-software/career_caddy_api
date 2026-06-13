"""Phase 4 federation — JobPost.source_deleted_at tombstone column.

Last Phase 4 readiness gap before the frontend retraction banner. The
column captures the time at which the originating instance broadcast
an ActivityPub ``Delete`` for the federated row. Local-origin rows
keep ``source_deleted_at IS NULL`` for their entire lifetime; only
inbound ``Delete`` activities from the row's ``source_instance`` flip
the column.

Nullable + no backfill — every existing row predates federation, so
there is no retroactive deletion authority to honor. The inbound
``Delete`` handler (api/views/federation.py::_handle_delete) writes
``timezone.now()`` on the first matching delivery and leaves it alone
on every replay so the audit trail records the original retraction
moment.

Indexed because the visibility layer + the audit-report queries
filter on ``source_deleted_at IS NULL`` / ``IS NOT NULL`` on every
list endpoint that surfaces federated rows. Without the index the
planner falls back to seq-scan once federated content meaningfully
populates.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0104_company_alias_self_fk_data"),
    ]

    operations = [
        migrations.AddField(
            model_name="jobpost",
            name="source_deleted_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]
