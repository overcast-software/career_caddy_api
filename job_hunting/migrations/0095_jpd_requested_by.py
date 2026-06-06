"""Phase 2.5 staff-on-behalf RBAC — add JobPostDiscovery.requested_by.

Records the request's authenticated principal — i.e. *who drove the
discovery write* — separately from `user` (the target the row is
attributed to). On every self-discover path the two are equal; they
diverge only when a staff-level API key (cc_auto's) writes a row for
some other user via the Phase 2.5 `discover_for_user_id` attribute.

Forward-migration backfill: every existing row pre-dates the column,
so we best-effort guess `requested_by_id = user_id` (every legacy
discovery was self-driven). The column is nullable so any row whose
attribution we can't infer stays unset rather than carrying a wrong
audit guess.
"""
from django.conf import settings
from django.db import migrations, models


def backfill_requested_by(apps, schema_editor):
    """Set requested_by_id = user_id for every pre-existing discovery."""
    JobPostDiscovery = apps.get_model("job_hunting", "JobPostDiscovery")
    JobPostDiscovery.objects.filter(requested_by_id__isnull=True).update(
        requested_by_id=models.F("user_id")
    )


def unbackfill_requested_by(apps, schema_editor):
    """Reverse: drop everything we backfilled. Safe — column is nullable."""
    JobPostDiscovery = apps.get_model("job_hunting", "JobPostDiscovery")
    JobPostDiscovery.objects.update(requested_by_id=None)


class Migration(migrations.Migration):
    dependencies = [
        ("job_hunting", "0094_jpd_forwarded_via"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="jobpostdiscovery",
            name="requested_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name="requested_job_post_discoveries",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(backfill_requested_by, unbackfill_requested_by),
    ]
