"""CC-77 #79 — Scrape integer PK -> 10-char NanoID PK (true PK swap).

Copies the self-FK mechanism proven on ``JobPost`` in
``0115_jobpost_nanoid_pk_swap`` (the only other swapped table with a
self-FK). FKs that reference ``scrape(id)``:

    scrape_status.scrape_id                          (CASCADE,  NOT NULL)
    scrape.source_scrape_id                          (self, SET_NULL, nullable)
    job_post_overwrite_decision.triggering_scrape_id (SET_NULL, nullable)
    job_post_description_decision.triggering_scrape_id (SET_NULL, nullable)

The self-FK makes ``SET CONSTRAINTS ALL IMMEDIATE`` necessary: the backfill
UPDATEs queue pending deferred-FK trigger events that would otherwise block
the first ``ALTER TABLE ... DROP CONSTRAINT`` ("pending trigger events").

No composite constraint/index rides on any of these FK columns — the two
``*_decision`` tables' ``Meta.indexes`` are on ``job_post_id``, never on
``triggering_scrape_id`` — so only the implicit FK btree indexes are
recreated (clean chosen names; Django does not track FK index names).

Mechanism + reverse semantics: see ``0115``.
"""

from __future__ import annotations

import logging

import job_hunting.models.nanoid_pk
from django.db import migrations, models

logger = logging.getLogger(__name__)

# (table, fk_column) for the 3 dependent FKs; the self-FK on scrape is
# handled inline because it lives on the parent table itself.
_DEPENDENT_FKS = [
    ("scrape_status", "scrape_id"),
    ("job_post_overwrite_decision", "triggering_scrape_id"),
    ("job_post_description_decision", "triggering_scrape_id"),
]


def backfill_nanoids(apps, schema_editor):
    """Mint a unique NanoID per ``scrape`` row, then repoint the self-FK and
    every dependent staging column with set-based joins."""
    from job_hunting.models.nanoid_pk import generate_nanoid

    with schema_editor.connection.cursor() as cur:
        cur.execute("SELECT id FROM scrape ORDER BY id")
        ids = [row[0] for row in cur.fetchall()]

        used: set[str] = set()
        for old_id in ids:
            nid = generate_nanoid()
            while nid in used:  # astronomically rare; keep it deterministic
                nid = generate_nanoid()
            used.add(nid)
            cur.execute(
                "UPDATE scrape SET new_id = %s WHERE id = %s", [nid, old_id]
            )

        # Self-FK: copy the parent's fresh NanoID into the child staging col.
        cur.execute(
            "UPDATE scrape c SET new_source_scrape_id = p.new_id "
            "FROM scrape p WHERE c.source_scrape_id = p.id"
        )

        for table, col in _DEPENDENT_FKS:
            cur.execute(
                f"UPDATE {table} t SET new_{col} = s.new_id "
                f"FROM scrape s WHERE t.{col} = s.id"
            )

    logger.info("0122 nanoid backfill: minted %s scrape ids.", len(ids))


ADD_STAGING_COLUMNS = """
ALTER TABLE scrape ADD COLUMN new_id varchar(10);
ALTER TABLE scrape ADD COLUMN new_source_scrape_id varchar(10);
ALTER TABLE scrape_status ADD COLUMN new_scrape_id varchar(10);
ALTER TABLE job_post_overwrite_decision ADD COLUMN new_triggering_scrape_id varchar(10);
ALTER TABLE job_post_description_decision ADD COLUMN new_triggering_scrape_id varchar(10);
"""

DROP_STAGING_COLUMNS = """
ALTER TABLE job_post_description_decision DROP COLUMN IF EXISTS new_triggering_scrape_id;
ALTER TABLE job_post_overwrite_decision DROP COLUMN IF EXISTS new_triggering_scrape_id;
ALTER TABLE scrape_status DROP COLUMN IF EXISTS new_scrape_id;
ALTER TABLE scrape DROP COLUMN IF EXISTS new_source_scrape_id;
ALTER TABLE scrape DROP COLUMN IF EXISTS new_id;
"""

SWAP_FORWARD = """
-- 0. Check deferred FK triggers NOW and run the rest in IMMEDIATE mode —
--    scrape's self-FK is DEFERRABLE INITIALLY DEFERRED, so the backfill
--    UPDATEs queue pending trigger events that would block DROP CONSTRAINT.
SET CONSTRAINTS ALL IMMEDIATE;

-- 1. Staging PK is fully backfilled: enforce NOT NULL before promotion.
ALTER TABLE scrape ALTER COLUMN new_id SET NOT NULL;

-- 2. Drop EVERY FK that references scrape (3 dependent + the self-FK).
--    confrelid='scrape' is the single predicate that sweeps them all.
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT conrelid::regclass AS tbl, conname
          FROM pg_constraint
         WHERE confrelid = 'scrape'::regclass AND contype = 'f'
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.tbl, r.conname);
    END LOOP;
END $$;

-- 3. Promote scrape's PK from the int id to the NanoID staging column.
DO $$
DECLARE cname text;
BEGIN
    SELECT conname INTO cname FROM pg_constraint
     WHERE conrelid = 'scrape'::regclass AND contype = 'p';
    EXECUTE format('ALTER TABLE scrape DROP CONSTRAINT %I', cname);
END $$;
ALTER TABLE scrape DROP COLUMN id;
ALTER TABLE scrape RENAME COLUMN new_id TO id;
ALTER TABLE scrape ADD CONSTRAINT scrape_pkey PRIMARY KEY (id);

-- 4. Repoint scrape's own self-FK column (nullable: no SET NOT NULL).
ALTER TABLE scrape DROP COLUMN source_scrape_id;
ALTER TABLE scrape RENAME COLUMN new_source_scrape_id TO source_scrape_id;

-- 5. Repoint every dependent FK column. scrape_status.scrape_id is
--    non-nullable (CASCADE); the two triggering_scrape_id are nullable.
ALTER TABLE scrape_status DROP COLUMN scrape_id;
ALTER TABLE scrape_status RENAME COLUMN new_scrape_id TO scrape_id;
ALTER TABLE scrape_status ALTER COLUMN scrape_id SET NOT NULL;
ALTER TABLE job_post_overwrite_decision DROP COLUMN triggering_scrape_id;
ALTER TABLE job_post_overwrite_decision RENAME COLUMN new_triggering_scrape_id TO triggering_scrape_id;
ALTER TABLE job_post_description_decision DROP COLUMN triggering_scrape_id;
ALTER TABLE job_post_description_decision RENAME COLUMN new_triggering_scrape_id TO triggering_scrape_id;

-- 6. Recreate the self-FK (NanoID -> NanoID) + its btree index.
ALTER TABLE scrape ADD CONSTRAINT scrape_source_scrape_id_fk
    FOREIGN KEY (source_scrape_id) REFERENCES scrape (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX scrape_source_scrape_id_idx ON scrape (source_scrape_id);

-- 7. Recreate every dependent FK (DEFERRABLE INITIALLY DEFERRED) + index.
ALTER TABLE scrape_status ADD CONSTRAINT scrape_status_scrape_id_fk
    FOREIGN KEY (scrape_id) REFERENCES scrape (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX scrape_status_scrape_id_idx ON scrape_status (scrape_id);
ALTER TABLE job_post_overwrite_decision ADD CONSTRAINT job_post_overwrite_decision_triggering_scrape_id_fk
    FOREIGN KEY (triggering_scrape_id) REFERENCES scrape (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX job_post_overwrite_decision_triggering_scrape_id_idx
    ON job_post_overwrite_decision (triggering_scrape_id);
ALTER TABLE job_post_description_decision ADD CONSTRAINT job_post_description_decision_triggering_scrape_id_fk
    FOREIGN KEY (triggering_scrape_id) REFERENCES scrape (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX job_post_description_decision_triggering_scrape_id_idx
    ON job_post_description_decision (triggering_scrape_id);
"""

SWAP_REVERSE = """
-- 0. IMMEDIATE constraint mode for this transaction (self-FK, as above).
SET CONSTRAINTS ALL IMMEDIATE;

-- 1. Integer staging columns on the parent + every FK-bearing table.
ALTER TABLE scrape ADD COLUMN old_int_id bigint;
ALTER TABLE scrape ADD COLUMN old_int_source_scrape_id bigint;
ALTER TABLE scrape_status ADD COLUMN old_int_scrape_id bigint;
ALTER TABLE job_post_overwrite_decision ADD COLUMN old_int_triggering_scrape_id bigint;
ALTER TABLE job_post_description_decision ADD COLUMN old_int_triggering_scrape_id bigint;

-- 2. Assign fresh sequential ints (1..N) deterministically by current id.
WITH numbered AS (
    SELECT id, row_number() OVER (ORDER BY id) AS rn FROM scrape
)
UPDATE scrape s SET old_int_id = n.rn FROM numbered n WHERE s.id = n.id;

-- 3. Repoint self-FK + dependent staging columns by joining on the NanoID.
UPDATE scrape c SET old_int_source_scrape_id = p.old_int_id
  FROM scrape p WHERE c.source_scrape_id = p.id;
UPDATE scrape_status t SET old_int_scrape_id = s.old_int_id
  FROM scrape s WHERE t.scrape_id = s.id;
UPDATE job_post_overwrite_decision t SET old_int_triggering_scrape_id = s.old_int_id
  FROM scrape s WHERE t.triggering_scrape_id = s.id;
UPDATE job_post_description_decision t SET old_int_triggering_scrape_id = s.old_int_id
  FROM scrape s WHERE t.triggering_scrape_id = s.id;

-- 4. Drop all FKs referencing scrape (dependent + self), catalog-resolved.
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT conrelid::regclass AS tbl, conname
          FROM pg_constraint
         WHERE confrelid = 'scrape'::regclass AND contype = 'f'
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.tbl, r.conname);
    END LOOP;
END $$;
DROP INDEX IF EXISTS scrape_source_scrape_id_idx;
DROP INDEX IF EXISTS scrape_status_scrape_id_idx;
DROP INDEX IF EXISTS job_post_overwrite_decision_triggering_scrape_id_idx;
DROP INDEX IF EXISTS job_post_description_decision_triggering_scrape_id_idx;

-- 5. Drop the NanoID PK and promote the int column to a real identity PK.
DO $$
DECLARE cname text;
BEGIN
    SELECT conname INTO cname FROM pg_constraint
     WHERE conrelid = 'scrape'::regclass AND contype = 'p';
    EXECUTE format('ALTER TABLE scrape DROP CONSTRAINT %I', cname);
END $$;
ALTER TABLE scrape DROP COLUMN id;
ALTER TABLE scrape RENAME COLUMN old_int_id TO id;
ALTER TABLE scrape ALTER COLUMN id SET NOT NULL;
ALTER TABLE scrape ADD CONSTRAINT scrape_pkey PRIMARY KEY (id);
ALTER TABLE scrape ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY;
SELECT setval(pg_get_serial_sequence('scrape', 'id'),
              (SELECT COALESCE(max(id), 1) FROM scrape));

-- 6. Self-FK column back to int.
ALTER TABLE scrape DROP COLUMN source_scrape_id;
ALTER TABLE scrape RENAME COLUMN old_int_source_scrape_id TO source_scrape_id;

-- 7. Dependent FK columns back to int (+ NOT NULL on the CASCADE relation).
ALTER TABLE scrape_status DROP COLUMN scrape_id;
ALTER TABLE scrape_status RENAME COLUMN old_int_scrape_id TO scrape_id;
ALTER TABLE scrape_status ALTER COLUMN scrape_id SET NOT NULL;
ALTER TABLE job_post_overwrite_decision DROP COLUMN triggering_scrape_id;
ALTER TABLE job_post_overwrite_decision RENAME COLUMN old_int_triggering_scrape_id TO triggering_scrape_id;
ALTER TABLE job_post_description_decision DROP COLUMN triggering_scrape_id;
ALTER TABLE job_post_description_decision RENAME COLUMN old_int_triggering_scrape_id TO triggering_scrape_id;

-- 8. Recreate the self-FK + index.
ALTER TABLE scrape ADD CONSTRAINT scrape_source_scrape_id_fk
    FOREIGN KEY (source_scrape_id) REFERENCES scrape (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX scrape_source_scrape_id_idx ON scrape (source_scrape_id);

-- 9. Recreate the dependent FKs + indexes.
ALTER TABLE scrape_status ADD CONSTRAINT scrape_status_scrape_id_fk
    FOREIGN KEY (scrape_id) REFERENCES scrape (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX scrape_status_scrape_id_idx ON scrape_status (scrape_id);
ALTER TABLE job_post_overwrite_decision ADD CONSTRAINT job_post_overwrite_decision_triggering_scrape_id_fk
    FOREIGN KEY (triggering_scrape_id) REFERENCES scrape (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX job_post_overwrite_decision_triggering_scrape_id_idx
    ON job_post_overwrite_decision (triggering_scrape_id);
ALTER TABLE job_post_description_decision ADD CONSTRAINT job_post_description_decision_triggering_scrape_id_fk
    FOREIGN KEY (triggering_scrape_id) REFERENCES scrape (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX job_post_description_decision_triggering_scrape_id_idx
    ON job_post_description_decision (triggering_scrape_id);
"""


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0121_jobapplication_nanoid_pk_swap"),
    ]

    operations = [
        migrations.RunSQL(
            sql=ADD_STAGING_COLUMNS,
            reverse_sql=DROP_STAGING_COLUMNS,
        ),
        migrations.RunPython(backfill_nanoids, migrations.RunPython.noop),
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(sql=SWAP_FORWARD, reverse_sql=SWAP_REVERSE),
            ],
            state_operations=[
                migrations.AlterField(
                    model_name="scrape",
                    name="id",
                    field=models.CharField(
                        default=job_hunting.models.nanoid_pk.generate_nanoid,
                        editable=False,
                        max_length=10,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
            ],
        ),
    ]
