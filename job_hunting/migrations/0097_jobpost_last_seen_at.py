"""Rolling-window dedupe enhancement — add JobPost.last_seen_at.

Background: ``find_duplicate``'s fingerprint fallback windows on
``created_at >= now() - 30d``. Long-tail roles that stay open past
30 days fall out of that window and no longer cross-platform-dedupe.
JP 1329 (Allstate Software Engineer — Product Security, 42 days old)
was the canonical repro: a fresh capture of the same role from a
different host failed to dedupe because the original was too old
under the static window.

Fix: switch the fingerprint window predicate to ``last_seen_at`` and
bump it on every write-path resolve-to-existing decision. The window
becomes rolling — a role stays dedupe-eligible as long as it keeps
being seen.

Schema step: add ``last_seen_at`` as a NOT NULL DateTimeField with a
Python-side default of ``timezone.now`` (no DB DEFAULT — Django emits
the default at INSERT time). AddField populates existing rows with
the migration-run timestamp first; the data step below corrects them.

Data step (``backfill``): for every existing JobPost set
``last_seen_at = GREATEST(created_at, max(scrape.created_at))``.
The expression captures the row's most-recent observed activity —
the same signal the runtime bump path will record going forward.
Falls back to ``created_at`` for rows with no linked scrapes.

Composite index: ``jobpost_fp_lastseen_idx`` on
``(content_fingerprint, -last_seen_at)`` so the windowed dedupe
query (``find_duplicate``'s fingerprint stage) doesn't seq-scan as
the table grows. Mirrors the shape of the existing
``jobpost_fp_recent_idx``.

Reversibility: the field drop reverses cleanly. The backfill cannot
be reversed (the pre-fix column didn't exist), but that's fine —
``reverse_code`` is a no-op and Django still runs the AddField/
RemoveField pair in the opposite direction.
"""

from django.db import migrations, models
from django.utils import timezone


def backfill_last_seen_at(apps, schema_editor):
    """Set last_seen_at = GREATEST(created_at, max(scrape.created_at)).

    Two-step:
    1. UPDATE every JobPost so last_seen_at = created_at as a baseline
       (replacing whatever timezone.now() value AddField inserted at
       migration time).
    2. For every JobPost that has at least one linked Scrape, set
       last_seen_at = max(scrape.created_at) when that exceeds
       last_seen_at.

    Done in raw SQL on the schema_editor's connection for speed —
    iterating per-row through the ORM on a multi-thousand-row JobPost
    table is the wrong order of magnitude. Both UPDATEs run in a
    single transaction (Django wraps RunPython in one automatically
    on PostgreSQL).
    """
    JobPost = apps.get_model("job_hunting", "JobPost")  # noqa: F841 (ORM keep-alive)
    with schema_editor.connection.cursor() as cursor:
        # Step 1: baseline every row to its own created_at. This wipes
        # the AddField default (timezone.now at migration time) so the
        # column reflects actual row history.
        cursor.execute(
            """
            UPDATE job_post
               SET last_seen_at = created_at
             WHERE created_at IS NOT NULL
            """
        )
        # Step 2: lift to max(scrape activity) when a linked scrape is
        # newer than the JP itself. Scrape has no ``created_at`` column
        # — the closest equivalent is ``scraped_at`` (set on completion
        # by the graph) with ``claimed_at`` as a fallback for rows that
        # got claimed by a runner but didn't finish. COALESCE picks the
        # first non-NULL per row; the outer MAX picks the most-recent
        # activity per JP. LEFT JOIN semantics via the inline subquery
        # — rows without linked scrapes simply keep their step-1 value.
        cursor.execute(
            """
            UPDATE job_post jp
               SET last_seen_at = GREATEST(jp.last_seen_at, s.max_activity)
              FROM (
                    SELECT job_post_id,
                           MAX(COALESCE(scraped_at, claimed_at)) AS max_activity
                      FROM scrape
                     WHERE job_post_id IS NOT NULL
                       AND (scraped_at IS NOT NULL OR claimed_at IS NOT NULL)
                  GROUP BY job_post_id
                  ) s
             WHERE jp.id = s.job_post_id
               AND s.max_activity IS NOT NULL
            """
        )


def reverse_backfill(apps, schema_editor):
    # No-op: the pre-fix column didn't exist, so there is no prior
    # state to restore. RemoveField in the operations' reverse pass
    # drops the column outright.
    pass


class Migration(migrations.Migration):

    # PostgreSQL refuses CREATE INDEX on a table that has pending
    # trigger events in the same transaction — and the AddField above
    # touches every job_post row, while the RunPython below issues two
    # UPDATEs against it. Wrapping all of that in one txn with the
    # AddIndex tail fails with "cannot CREATE INDEX … because it has
    # pending trigger events". Splitting txn boundaries via
    # atomic=False lets each operation commit before the next runs.
    atomic = False

    dependencies = [
        ("job_hunting", "0096_scrape_source_mode_captured_payload"),
    ]

    operations = [
        migrations.AddField(
            model_name="jobpost",
            name="last_seen_at",
            field=models.DateTimeField(db_index=True, default=timezone.now),
        ),
        migrations.RunPython(backfill_last_seen_at, reverse_backfill),
        migrations.AddIndex(
            model_name="jobpost",
            index=models.Index(
                fields=["content_fingerprint", "-last_seen_at"],
                name="jobpost_fp_lastseen_idx",
            ),
        ),
    ]
