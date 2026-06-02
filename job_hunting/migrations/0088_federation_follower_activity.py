"""Add FederationFollower + FederationActivity for ActivityPub Phase 5c.

Single migration for both models — they're a logical unit: every
follower lifecycle event (Follow / Accept / Undo) also lands a
FederationActivity row, so deploying them out of order would leave
the inbox handler with one table but not the other.

Hand-written rather than makemigrations-generated so the dependency
on ``settings.AUTH_USER_MODEL`` + the rationale stay explicit.
"""

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0087_actor"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="FederationFollower",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "actor_uri",
                    models.URLField(
                        help_text="Remote follower's Actor URI (e.g. https://mastodon.social/users/alice).",
                        max_length=512,
                    ),
                ),
                (
                    "inbox_uri",
                    models.URLField(
                        help_text="Remote actor's per-actor inbox (used for Accept + targeted deliveries).",
                        max_length=512,
                    ),
                ),
                (
                    "shared_inbox_uri",
                    models.URLField(
                        blank=True,
                        help_text=(
                            "Optional shared inbox URL from the remote actor's "
                            "endpoints. 5d dispatch coalesces fan-out to shared "
                            "inbox when present."
                        ),
                        max_length=512,
                        null=True,
                    ),
                ),
                (
                    "instance_host",
                    models.CharField(
                        db_index=True,
                        help_text="Host portion of actor_uri — indexed for per-instance rate limiting + dispatch coalescing.",
                        max_length=255,
                    ),
                ),
                (
                    "accepted_at",
                    models.DateTimeField(
                        blank=True,
                        help_text="When the outbound Accept(Follow) successfully delivered.",
                        null=True,
                    ),
                ),
                (
                    "unfollowed_at",
                    models.DateTimeField(
                        blank=True,
                        help_text="When Undo(Follow) was received. Null = currently following.",
                        null=True,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "local_user",
                    models.ForeignKey(
                        help_text="Local user being followed (the followee).",
                        on_delete=models.deletion.CASCADE,
                        related_name="federation_followers",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "federation_followers",
            },
        ),
        migrations.AddConstraint(
            model_name="FederationFollower",
            constraint=models.UniqueConstraint(
                fields=("local_user", "actor_uri"),
                name="federation_follower_unique_local_remote",
            ),
        ),
        migrations.AddIndex(
            model_name="FederationFollower",
            index=models.Index(fields=["instance_host"], name="fed_fwr_inst_host_idx"),
        ),
        migrations.AddIndex(
            model_name="FederationFollower",
            index=models.Index(fields=["unfollowed_at"], name="fed_fwr_unfwd_at_idx"),
        ),
        migrations.CreateModel(
            name="FederationActivity",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "direction",
                    models.CharField(
                        choices=[("inbound", "Inbound"), ("outbound", "Outbound")],
                        db_index=True,
                        max_length=16,
                    ),
                ),
                (
                    "activity_type",
                    models.CharField(
                        choices=[
                            ("Follow", "Follow"),
                            ("Undo", "Undo"),
                            ("Accept", "Accept"),
                            ("Create", "Create"),
                            ("Other", "Other"),
                        ],
                        db_index=True,
                        default="Other",
                        max_length=32,
                    ),
                ),
                (
                    "activity_id",
                    models.URLField(
                        db_index=True,
                        help_text=(
                            "The activity's ``id`` field — peer-asserted for "
                            "inbound, ours for outbound. Combined with "
                            "``direction`` for replay dedupe."
                        ),
                        max_length=512,
                    ),
                ),
                (
                    "actor_uri",
                    models.URLField(
                        db_index=True,
                        help_text="The activity's ``actor`` field (who performed the action).",
                        max_length=512,
                    ),
                ),
                (
                    "target_uri",
                    models.URLField(
                        blank=True,
                        help_text="For Follow/Undo, the ``object`` URI. Nullable.",
                        max_length=512,
                        null=True,
                    ),
                ),
                (
                    "body",
                    models.TextField(
                        help_text="Full activity JSON (canonical text). Source of truth for replay + audit.",
                    ),
                ),
                (
                    "signature_payload",
                    models.TextField(
                        blank=True,
                        help_text="Verified Signature header for inbound; null for outbound.",
                        null=True,
                    ),
                ),
                (
                    "received_at",
                    models.DateTimeField(
                        blank=True,
                        help_text="When the inbound activity arrived. Null for outbound rows.",
                        null=True,
                    ),
                ),
                (
                    "delivered_at",
                    models.DateTimeField(
                        blank=True,
                        help_text="When the outbound activity successfully delivered. Null for inbound rows or failed outbound.",
                        null=True,
                    ),
                ),
                (
                    "delivery_status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("accepted", "Accepted"),
                            ("rejected", "Rejected"),
                            ("failed", "Failed"),
                        ],
                        default="accepted",
                        help_text=(
                            "Outbound: pending/accepted/rejected/failed based on "
                            "peer response. Inbound: ``accepted`` after signature "
                            "verification passes."
                        ),
                        max_length=16,
                    ),
                ),
                (
                    "delivery_error",
                    models.TextField(
                        blank=True,
                        help_text="Status code + body snippet on outbound failure; null otherwise.",
                        null=True,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "local_user",
                    models.ForeignKey(
                        blank=True,
                        help_text="Scoped to a local user when applicable; null for instance-level activities.",
                        null=True,
                        on_delete=models.deletion.SET_NULL,
                        related_name="federation_activities",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "federation_activities",
            },
        ),
        migrations.AddConstraint(
            model_name="FederationActivity",
            constraint=models.UniqueConstraint(
                fields=("direction", "activity_id"),
                name="federation_activity_unique_direction_id",
            ),
        ),
        migrations.AddIndex(
            model_name="FederationActivity",
            index=models.Index(
                fields=["activity_type", "-created_at"],
                name="fed_act_type_ctime_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="FederationActivity",
            index=models.Index(
                fields=["actor_uri"],
                name="fed_act_actor_uri_idx",
            ),
        ),
    ]
