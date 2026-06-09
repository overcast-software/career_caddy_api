"""Phase C of the dedupe redesign — repost relation.

Adds ``JobPost.reposted_from``, a self-FK distinct from
``duplicate_of``. Where ``duplicate_of`` means "same hiring cycle —
collapse this row into its canonical sibling", ``reposted_from`` means
"different hiring cycle — same role re-posted later, keep both rows
independently queryable but record the link".

Pure schema change. No data backfill — the column starts NULL for
every existing row. Operators promote a JobPost from
``duplicate_of`` to ``reposted_from`` by re-issuing the
``mark-duplicate-of`` verb with ``relation: "repost"`` (Phase C
verb extension).

The new ``mark_repost`` action enum value on ``DuplicateAnnotation``
is added in a sibling migration in the same release — both ship
together so the verb handler can record either action consistently.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0099_jobpost_normalized_fingerprint"),
    ]

    operations = [
        migrations.AddField(
            model_name="jobpost",
            name="reposted_from",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name="reposts",
                to="job_hunting.jobpost",
            ),
        ),
        migrations.AlterField(
            model_name="duplicateannotation",
            name="action",
            field=models.CharField(
                choices=[
                    ("mark", "mark"),
                    ("unlink", "unlink"),
                    ("promote", "promote"),
                    ("historical", "historical"),
                    ("federated_merge", "federated_merge"),
                    ("company_merge", "company_merge"),
                    ("mark_repost", "mark_repost"),
                ],
                max_length=16,
            ),
        ),
    ]
