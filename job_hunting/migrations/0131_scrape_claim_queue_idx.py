"""Index the claim-next hold queue (CC-96).

POST /api/v1/scrapes/claim-next/ runs::

    WHERE status='hold' AND claimed_at IS NULL
    ORDER BY created_at ASC NULLS FIRST, id
    LIMIT 1
    FOR UPDATE SKIP LOCKED

Before this migration there was no index covering that predicate — the
claim did a Seq Scan + Sort over the whole ``scrape`` table, which grows
unbounded (``prune_scrape_html`` nulls html but never deletes rows). As
the table grows the claim slows, and under contention a slow claim can
run up to the gunicorn ``timeout=120`` and get the worker SIGKILLed
mid-transaction — the CC-96 claim-next wedge.

A scrape is a scrape (CC-114): the hold queue is a single FIFO with no
attended partition, so the index leads with the ORDER BY columns directly.
The partial composite index matches the query exactly:
  * ``condition`` prunes to the active hold queue only (tiny), so the
    index stays small regardless of total scrape volume;
  * ``created_at`` NULLS FIRST + ``id`` match the ORDER BY so the planner
    satisfies it index-only (no Sort).

Built with CREATE INDEX CONCURRENTLY (``atomic = False``) so it never
takes a write lock on the prod ``scrape`` table during the build.
"""

from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("job_hunting", "0130_jobpost_apply_url_hash_idx"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="scrape",
            index=models.Index(
                models.F("created_at").asc(nulls_first=True),
                models.F("id"),
                name="scrape_claim_queue_idx",
                condition=models.Q(status="hold", claimed_at__isnull=True),
            ),
        ),
    ]
