"""CC-218 — Summary.job_post_id IntegerField -> NanoID ForeignKey(JobPost).

The CC-57 straggler: when JobPost's PK swapped from int to a 10-char NanoID
(migration 0115), every dependent FK column was repointed onto the fresh
NanoID via a set-based join while both the old int PK and new NanoID coexisted
on ``job_post`` (see 0115 ``_DEPENDENT_FKS``). ``summary.job_post_id`` was a
plain ``IntegerField`` — NOT a registered FK — so it was skipped. Its int
values still point at the pre-CC-57 integer PK id-space, which 0115 destroyed;
CC-57 preserved NO old-int -> NanoID mapping table ("Old integer object ids are
deliberately NOT preserved" — 0115 docstring). So those ints are unrecoverable.

Data-migration decision: **null the unmappable orphan ints.** There is no
mapping to remap through, and a bigint can never satisfy a varchar(10) NanoID
FK — every existing non-null value is stale. Nulling is the only correct move
(and the summary is regenerable). ``on_delete=SET_NULL`` on the new FK matches.

Mechanism mirrors 0115: hand-written DB surgery (a column TYPE change +
add-FK is not cleanly auto-generated) wrapped in ``SeparateDatabaseAndState``
so Django's project state gets the clean IntegerField -> ForeignKey edit while
the DB does the null + retype + constraint. The autodetector's naive
RemoveField+AddField is avoided (it would drop/recreate the column and churn).
"""

from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models

# Forward: null the orphaned int values, retype the column to the NanoID
# varchar(10), then add the FK constraint + btree index (matching how 0115
# recreated the other dependent job_post FKs).
FORWARD_SQL = """
-- 1. Null every stale int job_post_id — all values point at the destroyed
--    pre-CC-57 integer id-space (no mapping preserved). SET_NULL on the FK.
UPDATE summary SET job_post_id = NULL WHERE job_post_id IS NOT NULL;

-- 2. Retype the (now all-NULL) column from integer to the NanoID varchar(10).
ALTER TABLE summary
    ALTER COLUMN job_post_id TYPE varchar(10) USING job_post_id::varchar(10);

-- 3. Add the FK onto job_post(id) + its btree index (DEFERRABLE INITIALLY
--    DEFERRED, matching Django + the 0115 pattern).
ALTER TABLE summary ADD CONSTRAINT summary_job_post_id_fk
    FOREIGN KEY (job_post_id) REFERENCES job_post (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX summary_job_post_id_idx ON summary (job_post_id);
"""

# Reverse: drop the FK + index and retype back to a bare integer column. The
# nulled values are NOT restored (they were unrecoverable stale ints); the
# reverse yields a structurally-valid IntegerField, all-NULL — the same
# "structurally valid, not value-preserving" contract as 0115's reverse.
REVERSE_SQL = """
DROP INDEX IF EXISTS summary_job_post_id_idx;
ALTER TABLE summary DROP CONSTRAINT IF EXISTS summary_job_post_id_fk;
ALTER TABLE summary
    ALTER COLUMN job_post_id TYPE integer USING NULL;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0136_resume_file_field"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(sql=FORWARD_SQL, reverse_sql=REVERSE_SQL),
            ],
            state_operations=[
                # Django state: the IntegerField becomes a ForeignKey. The
                # column name stays ``job_post_id`` (Django's FK default), so
                # no rename — existing reads of ``summary.job_post_id`` are
                # unaffected.
                migrations.RemoveField(
                    model_name="summary",
                    name="job_post_id",
                ),
                migrations.AddField(
                    model_name="summary",
                    name="job_post",
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="summaries",
                        to="job_hunting.jobpost",
                    ),
                ),
            ],
        ),
    ]
