"""PACA CC-114: drop Scrape.attended.

Attended-scrape routing is removed — the claim queue is a single FIFO again
and the per-scrape ``attended`` flag (added by 0110) is dead. RemoveField
drops the column and its db_index.
"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("job_hunting", "0127_delete_orphaned_attended_hold_schedule"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="scrape",
            name="attended",
        ),
    ]
