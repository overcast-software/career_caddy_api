"""Phase 6a — Company / Organization actors schema.

Wires the schema shape called out in
``notes.org/Plans/PLAN Fediverse Phase 6/Phase 6a — Company Organization actors``:

1. ``Actor.company`` — nullable FK to Company. A Company-actor row
   carries ``company`` set + ``user`` NULL. The mutual-exclusivity
   ``CheckConstraint`` ``actor_user_company_mutually_exclusive``
   enforces "at most one of (user, company) is set" — Person actors
   set ``user`` only; Organization actors set ``company`` only; the
   Instance Actor leaves both NULL.
2. ``Actor.avatar_url`` — URLField max 500, nullable. Reused by 7a's
   Person-actor profile UI.
3. ``Company.slug`` — SlugField max 80, unique, nullable. The
   WebFinger / Actor-URI handle (``acct:<slug>@<host>``). Distinct
   from ``Company.name_slug``, which is the dedupe key derived from
   ``slug(strip_corp_suffix(name))``. ``backfill_company_slugs``
   populates existing rows after this migration runs.
4. ``Company.federation_enabled`` — BooleanField default False (Q2
   in the Phase 6 plan: opt-in per Company so freshly-scraped rows
   don't auto-publish to the fediverse before employer claim).

Hand-written rather than makemigrations-generated so the constraint
rationale travels with the file.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0105_jobpost_source_deleted_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="actor",
            name="company",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.CASCADE,
                related_name="federation_actors",
                to="job_hunting.company",
            ),
        ),
        migrations.AddField(
            model_name="actor",
            name="avatar_url",
            field=models.URLField(blank=True, max_length=500, null=True),
        ),
        migrations.AddField(
            model_name="company",
            name="slug",
            field=models.SlugField(
                blank=True, max_length=80, null=True, unique=True
            ),
        ),
        migrations.AddField(
            model_name="company",
            name="federation_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddConstraint(
            model_name="actor",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(user__isnull=True)
                    | models.Q(company__isnull=True)
                ),
                name="actor_user_company_mutually_exclusive",
            ),
        ),
    ]
