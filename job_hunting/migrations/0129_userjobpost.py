# BACK-105 — UserJobPost owner↔post join (AUTO-18 multi-user forward@).
#
# Hand-trimmed to contain ONLY the UserJobPost operations. A bare
# `makemigrations` also swept in pre-existing model/migration drift on main
# (CompanyAlias / Federation* / Actor index-name + auto-field
# representation churn unrelated to this change); those operations are
# intentionally excluded so this migration is scoped to BACK-105.
#
# The `job_post` FK targets the NanoID string-PK JobPost (CC-77); Django
# derives the matching varchar FK column from the target PK automatically.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0128_remove_scrape_attended"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="UserJobPost",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "role",
                    models.CharField(
                        choices=[("owner", "owner"), ("member", "member")],
                        default="owner",
                        max_length=16,
                    ),
                ),
                ("source", models.CharField(blank=True, max_length=32, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "job_post",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="user_memberships",
                        to="job_hunting.jobpost",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="user_job_posts",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "user_job_post",
                "unique_together": {("job_post", "user")},
            },
        ),
    ]
