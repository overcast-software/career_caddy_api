"""Phase 6b — Company-actor Follow handshake schema.

Adds a nullable ``company`` FK to ``FederationFollower`` so a Follow
targeting a Company actor (``/companies/<slug>/``) materializes a row
keyed off the Company. Loosens ``local_user`` to nullable for the
symmetric Company-only path. The followee identity is enforced as
"at least one set" via a check constraint, and uniqueness is split
into two partial-unique indexes (one per followee column) so the
Person-actor path's existing ``(local_user, actor_uri)`` invariant
carries through without colliding with NULL Company rows. Rows that
set BOTH columns participate in both indexes — that's the Phase 6b
discovery-channel shape (a local user subscribing to a Company actor).

Hand-written rather than makemigrations-generated so the rationale +
the constraint shape stay explicit.
"""

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0107_phase6b_federation_source"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Loosen local_user → nullable so the Company-only path can land
        # rows without a User. Existing rows stay valid (every legacy
        # row has local_user set).
        migrations.AlterField(
            model_name="federationfollower",
            name="local_user",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Local user being followed (Person-actor followee), OR the "
                    "local user subscribing to a Company actor (when ``company`` "
                    "is also set). NULL when the followee is a Company alone."
                ),
                null=True,
                on_delete=models.deletion.CASCADE,
                related_name="federation_followers",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        # New company FK — Organization-actor followee.
        migrations.AddField(
            model_name="federationfollower",
            name="company",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Phase 6b — Company being followed (Organization-actor "
                    "followee). NULL for Person-actor follows. Mutually "
                    "exclusive with ``local_user``."
                ),
                null=True,
                on_delete=models.deletion.CASCADE,
                related_name="federation_followers",
                to="job_hunting.Company",
            ),
        ),
        # Drop the old unconditional unique; replace with two partial
        # uniques + an XOR check.
        migrations.RemoveConstraint(
            model_name="federationfollower",
            name="federation_follower_unique_local_remote",
        ),
        migrations.AddConstraint(
            model_name="federationfollower",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(local_user__isnull=False)
                    | models.Q(company__isnull=False)
                ),
                name="federation_follower_followee_required",
            ),
        ),
        migrations.AddConstraint(
            model_name="federationfollower",
            constraint=models.UniqueConstraint(
                fields=("local_user", "actor_uri"),
                condition=models.Q(local_user__isnull=False),
                name="federation_follower_unique_local_remote",
            ),
        ),
        migrations.AddConstraint(
            model_name="federationfollower",
            constraint=models.UniqueConstraint(
                fields=("company", "actor_uri"),
                condition=models.Q(company__isnull=False),
                name="federation_follower_unique_company_remote",
            ),
        ),
        migrations.AddIndex(
            model_name="federationfollower",
            index=models.Index(fields=["company"], name="fed_fwr_company_idx"),
        ),
    ]
