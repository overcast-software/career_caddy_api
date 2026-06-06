"""Phase 2.5 catchall mail ingest — add JobPostDiscovery.forwarded_via_address.

Records which catchall To-address a user forwarded a listing to (e.g.
`dough@careercaddy.online`). Required when the discovery's `source ==
"email-forward"`; null for every other source. See
`job_hunting/models/job_post_discovery.py` for the column docstring.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("job_hunting", "0093_seed_linkedin_job_data_selectors"),
    ]

    operations = [
        migrations.AddField(
            model_name="jobpostdiscovery",
            name="forwarded_via_address",
            field=models.CharField(blank=True, max_length=254, null=True),
        ),
    ]
