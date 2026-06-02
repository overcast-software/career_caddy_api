"""Phase 5d outbound dispatch — schema additions to FederationActivity.

Adds the three columns the dispatcher needs:

- ``retry_count`` (PositiveIntegerField, default 0) — attempts so far.
- ``next_attempt_at`` (DateTimeField, nullable, indexed) — earliest
  eligible re-dispatch time; also doubles as the in-flight marker for
  the periodic sweep.
- Two new ``delivery_status`` choices: ``delivered`` (5d success terminal,
  distinct from 5c's inbound ``accepted``) and ``dead_letter`` (terminal
  failure after exhausting the backoff schedule).
- Two new ``activity_type`` choices: ``Update`` and ``Delete``.

The status / activity choice extensions are pure Python-side enum
additions — Postgres stores them as plain CharField values, so no DDL
beyond the new column additions. Existing rows keep their current
``delivery_status`` ("accepted" / "pending" / etc.).
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0088_federation_follower_activity"),
    ]

    operations = [
        migrations.AddField(
            model_name="federationactivity",
            name="retry_count",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Number of dispatch attempts so far (0 = first attempt has not yet run).",
            ),
        ),
        migrations.AddField(
            model_name="federationactivity",
            name="next_attempt_at",
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                null=True,
                help_text=(
                    "When the row is eligible for the next dispatch attempt. Set to now() "
                    "on enqueue; pushed out per ACTIVITYPUB_DISPATCH_RETRY_BACKOFF_SECONDS "
                    "on transient failure. Null for terminal rows."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="federationactivity",
            name="delivery_status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("accepted", "Accepted"),
                    ("delivered", "Delivered"),
                    ("rejected", "Rejected"),
                    ("failed", "Failed"),
                    ("dead_letter", "Dead Letter"),
                ],
                default="accepted",
                max_length=16,
                help_text=(
                    "Outbound: pending/accepted/rejected/failed based on peer response. "
                    "Inbound: ``accepted`` after signature verification passes."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="federationactivity",
            name="activity_type",
            field=models.CharField(
                choices=[
                    ("Follow", "Follow"),
                    ("Undo", "Undo"),
                    ("Accept", "Accept"),
                    ("Create", "Create"),
                    ("Update", "Update"),
                    ("Delete", "Delete"),
                    ("Other", "Other"),
                ],
                db_index=True,
                default="Other",
                max_length=32,
            ),
        ),
        # Drop the (direction, activity_id) constraint and re-add it as
        # (direction, activity_id, target_uri) — 5d fanout needs to
        # materialize one row per follower inbox, so the same activity_id
        # repeats across rows with distinct target_uris.
        migrations.RemoveConstraint(
            model_name="federationactivity",
            name="federation_activity_unique_direction_id",
        ),
        migrations.AddConstraint(
            model_name="federationactivity",
            constraint=models.UniqueConstraint(
                fields=["direction", "activity_id", "target_uri"],
                name="federation_activity_unique_direction_id_target",
            ),
        ),
    ]
