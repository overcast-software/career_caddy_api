from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0065_backfill_discoveries_user_2"),
    ]

    operations = [
        migrations.CreateModel(
            name="JobPostDescriptionDecision",
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
                    "job_post",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="description_decisions",
                        to="job_hunting.jobpost",
                    ),
                ),
                (
                    "triggering_scrape",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="description_decisions",
                        to="job_hunting.scrape",
                    ),
                ),
                ("existing_description_hash", models.CharField(max_length=64)),
                ("new_description_hash", models.CharField(max_length=64)),
                ("existing_word_count", models.IntegerField()),
                ("new_word_count", models.IntegerField()),
                (
                    "existing_source",
                    models.CharField(blank=True, default="", max_length=32),
                ),
                (
                    "new_source",
                    models.CharField(blank=True, default="", max_length=32),
                ),
                ("choice", models.CharField(max_length=16)),
                ("confidence", models.CharField(max_length=8)),
                ("reasoning", models.TextField(blank=True, default="")),
                (
                    "model_name",
                    models.CharField(blank=True, default="", max_length=128),
                ),
                ("duration_ms", models.IntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "db_table": "job_post_description_decision",
            },
        ),
        migrations.AddIndex(
            model_name="jobpostdescriptiondecision",
            index=models.Index(
                fields=["job_post", "-created_at"],
                name="jp_desc_dec_job_post_created_idx",
            ),
        ),
    ]
