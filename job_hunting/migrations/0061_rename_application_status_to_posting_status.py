"""Rename JobPost.application_status → JobPost.posting_status.

The original name collided with JobApplication.status (which is the
*user's* per-application status — Applied, Interview Scheduled,
Rejected). The two concepts read identically as 'application status'
to a future reader, so we move the JobPost-level field to a noun
scoped to the post itself: 'posting_status'.

RenameField preserves the column data and the index. No backfill or
data touch — the values stay open / closed / NULL exactly as before.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0060_jobpost_application_status"),
    ]

    operations = [
        migrations.RenameField(
            model_name="jobpost",
            old_name="application_status",
            new_name="posting_status",
        ),
    ]
