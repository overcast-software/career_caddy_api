"""Drop Scrape.apply_url_from_hint — collapse two-channel apply_url to one.

The column was introduced in 0074 to keep extension-supplied apply URLs
separate from agent-resolved ones (Camoufox ResolveApplyUrl). With
Camoufox being phased out, the two channels collapse into a single
JobPost.apply_url column written by whoever extracts it. The
cross-platform link surfaces via a direct JP→JP join, not a Scrape join.

See notes.org Plans/Cross-platform dedup — collapse two-channel
apply_url into JobPost.apply_url.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('job_hunting', '0077_refresh_linkedin_apply_button_selectors'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='scrape',
            name='apply_url_from_hint',
        ),
    ]
