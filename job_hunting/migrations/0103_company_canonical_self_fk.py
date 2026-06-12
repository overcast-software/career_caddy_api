"""Phase A self-FK alias schema on Company.

Adds:

1. ``Company.canonical`` self-FK (NULL = this row IS canonical). An
   alias is a Company whose ``canonical_id`` points at the true
   Company. ``on_delete=SET_NULL`` so deleting the canonical strands
   its aliases rather than cascading them — staff can re-alias.
2. ``CheckConstraint`` ``company_canonical_not_self`` blocking a
   self-loop at the DB layer. The service verb
   ``Company.mark_as_alias_of`` already rejects self-target in
   Python; the constraint covers admin / shell / migration writes.
3. Renames ``CompanyAlias.company.related_name`` from ``aliases`` to
   ``legacy_aliases`` so the two reverse accessors don't collide.
   The model itself is retired in Phase C (after the frontend
   reconciles); this rename is the smallest move that makes the new
   self-FK use the natural ``related_name="aliases"`` Doug picked.

Phase A keeps CompanyAlias rows live. The data migration that
mirrors them into Company self-FK rows lands in 0104; this one is
the schema-only step.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0102_scrape_failure_reason"),
    ]

    operations = [
        migrations.AddField(
            model_name="company",
            name="source",
            field=models.CharField(
                blank=True,
                choices=[
                    ("extraction", "extraction"),
                    ("manual", "manual"),
                    ("backfill", "backfill"),
                ],
                max_length=32,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="company",
            name="name_slug",
            field=models.CharField(
                blank=True, db_index=True, max_length=255, null=True
            ),
        ),
        migrations.AddField(
            model_name="company",
            name="created_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="company",
            name="canonical",
            field=models.ForeignKey(
                blank=True,
                db_index=True,
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name="aliases",
                to="job_hunting.company",
            ),
        ),
        migrations.AddConstraint(
            model_name="company",
            constraint=models.CheckConstraint(
                condition=models.Q(canonical__isnull=True)
                | ~models.Q(canonical=models.F("id")),
                name="company_canonical_not_self",
            ),
        ),
        migrations.AlterField(
            model_name="companyalias",
            name="company",
            field=models.ForeignKey(
                on_delete=models.deletion.CASCADE,
                related_name="legacy_aliases",
                to="job_hunting.company",
            ),
        ),
    ]
