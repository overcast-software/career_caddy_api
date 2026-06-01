"""Add Actor model for ActivityPub federation (Phase 5a).

Q1 (resolved 2026-06-01): separate ``Actor`` table rather than
augmenting ``User``. The Instance Actor mandated by Mastodon's
authorized-fetch mode carries no ``user_id``; future Service /
Application actors don't either. ``Actor.user`` is therefore a nullable
FK and the row owns its own federation surface (preferredUsername,
publicKey, privateKey, type).

Hand-written rather than makemigrations-generated so the
``settings.AUTH_USER_MODEL`` dependency stays explicit and the rationale
travels with the migration file.
"""

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0086_register_scrape_claim_sweep_schedule"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Actor",
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
                    "type",
                    models.CharField(
                        choices=[
                            ("Person", "Person"),
                            ("Service", "Service"),
                            ("Group", "Group"),
                            ("Application", "Application"),
                            ("Organization", "Organization"),
                        ],
                        default="Person",
                        max_length=32,
                    ),
                ),
                (
                    "preferred_username",
                    models.SlugField(
                        help_text=(
                            "WebFinger / Actor URI handle. Person actors "
                            "mirror User.username; Instance Actor uses "
                            "'instance'."
                        ),
                        max_length=150,
                        unique=True,
                    ),
                ),
                ("public_key_pem", models.TextField(blank=True, null=True)),
                ("private_key_pem", models.TextField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=models.deletion.CASCADE,
                        related_name="federation_actors",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "federation_actors",
            },
        ),
    ]
