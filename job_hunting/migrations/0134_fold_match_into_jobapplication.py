# CC-135 refold — drop the standalone MatchRequest model and fold the agentic
# JobPost-match flow into JobApplication.
#
# MatchRequest (0133) never reached prod (prod deployed at api 590db58, which
# predates 0133), so on prod this deploy is a create-then-drop within one
# migration run and no data is lost. Dev DBs that floated to main and applied
# 0133 get a clean DeleteModel here, keeping the migration graph linear and the
# schema consistent — the deliberate choice over deleting 0133 outright (which
# would strand an orphan table + django_migrations row on those dev DBs).
#
# Hand-trimmed to ONLY this refold's two operations. A bare `makemigrations`
# also sweeps in pre-existing model/migration drift on main (CompanyAlias /
# Federation* / Actor index-name + auto-field churn unrelated to this change);
# those are intentionally excluded — same convention as 0129 / 0133.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0133_matchrequest"),
    ]

    operations = [
        migrations.DeleteModel(
            name="MatchRequest",
        ),
        migrations.AddField(
            model_name="jobapplication",
            name="match_context",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
