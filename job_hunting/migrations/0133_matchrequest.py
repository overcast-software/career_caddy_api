# CC-135 — MatchRequest: staff-gated agentic JobPost lookup.
#
# Hand-trimmed to contain ONLY the MatchRequest CreateModel. A bare
# `makemigrations` also sweeps in pre-existing model/migration drift on main
# (CompanyAlias / Federation* / Actor index-name + auto-field churn unrelated
# to this change); those are intentionally excluded so this migration stays
# scoped to CC-135 — same convention as 0129_userjobpost.
#
# `id` is the 10-char NanoID string PK from NanoIDModel (CC-77). The
# `result_job_post` FK targets the NanoID string-PK JobPost; Django derives the
# matching varchar FK column from the target PK automatically.

import django.db.models.deletion
import job_hunting.models.nanoid_pk
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0132_scrapeprofile_tier0_hit_count"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="MatchRequest",
            fields=[
                (
                    "id",
                    models.CharField(
                        default=job_hunting.models.nanoid_pk.generate_nanoid,
                        editable=False,
                        max_length=10,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("url", models.CharField(max_length=2000)),
                ("referrer", models.CharField(blank=True, max_length=2000)),
                ("page_title", models.CharField(blank=True, max_length=500)),
                ("text_excerpt", models.TextField(blank=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "pending"),
                            ("done", "done"),
                            ("failed", "failed"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("confidence", models.FloatField(blank=True, null=True)),
                ("rationale", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="match_requests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "result_job_post",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="match_requests",
                        to="job_hunting.jobpost",
                    ),
                ),
            ],
            options={
                "db_table": "match_request",
                "ordering": ["-created_at", "-id"],
            },
        ),
    ]
