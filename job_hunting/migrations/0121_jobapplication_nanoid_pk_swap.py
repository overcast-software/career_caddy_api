"""CC-77 #79 — JobApplication integer PK -> 10-char NanoID PK (true PK swap).

Copies the dependent-FK mechanism proven on ``Skill`` in
``0114_skill_nanoid_pk_swap``. Two FKs reference ``job_application(id)``
(both via ``db_column="application_id"``):

    job_application_status.application_id   (CASCADE,  NOT NULL)
    question.application_id                 (SET_NULL, nullable)

The CASCADE column is restored NOT NULL; the SET_NULL column stays
nullable. No composite constraint/index rides on either ``application_id``
column, so only the implicit FK btree indexes are recreated (clean chosen
names — Django does not track FK index names).

Mechanism + reverse semantics: see ``0114``/``0116``.
"""

from __future__ import annotations

import logging

import job_hunting.models.nanoid_pk
from django.db import migrations, models

logger = logging.getLogger(__name__)

# (table, fk_column) for the two dependent FKs.
_DEPENDENT_FKS = [
    ("job_application_status", "application_id"),
    ("question", "application_id"),
]


def backfill_nanoids(apps, schema_editor):
    """Mint a unique NanoID per ``job_application`` row, then repoint both
    dependent ``application_id`` staging columns."""
    from job_hunting.models.nanoid_pk import generate_nanoid

    with schema_editor.connection.cursor() as cur:
        cur.execute("SELECT id FROM job_application ORDER BY id")
        ids = [row[0] for row in cur.fetchall()]

        used: set[str] = set()
        for old_id in ids:
            nid = generate_nanoid()
            while nid in used:  # astronomically rare; keep it deterministic
                nid = generate_nanoid()
            used.add(nid)
            cur.execute(
                "UPDATE job_application SET new_id = %s WHERE id = %s",
                [nid, old_id],
            )

        for table, col in _DEPENDENT_FKS:
            cur.execute(
                f"UPDATE {table} t SET new_{col} = ja.new_id "
                f"FROM job_application ja WHERE t.{col} = ja.id"
            )

    logger.info("0121 nanoid backfill: minted %s job_application ids.", len(ids))


ADD_STAGING_COLUMNS = """
ALTER TABLE job_application ADD COLUMN new_id varchar(10);
ALTER TABLE job_application_status ADD COLUMN new_application_id varchar(10);
ALTER TABLE question ADD COLUMN new_application_id varchar(10);
"""

DROP_STAGING_COLUMNS = """
ALTER TABLE question DROP COLUMN IF EXISTS new_application_id;
ALTER TABLE job_application_status DROP COLUMN IF EXISTS new_application_id;
ALTER TABLE job_application DROP COLUMN IF EXISTS new_id;
"""

SWAP_FORWARD = """
-- 1. Staging PK is fully backfilled: enforce NOT NULL before promotion.
ALTER TABLE job_application ALTER COLUMN new_id SET NOT NULL;

-- 2. Drop every FK that references job_application (catalog-resolved;
--    confrelid filter sweeps both dependent FKs).
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT conrelid::regclass AS tbl, conname
          FROM pg_constraint
         WHERE confrelid = 'job_application'::regclass AND contype = 'f'
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.tbl, r.conname);
    END LOOP;
END $$;

-- 3. Promote job_application's PK from the int id to the NanoID column.
DO $$
DECLARE cname text;
BEGIN
    SELECT conname INTO cname FROM pg_constraint
     WHERE conrelid = 'job_application'::regclass AND contype = 'p';
    EXECUTE format('ALTER TABLE job_application DROP CONSTRAINT %I', cname);
END $$;
ALTER TABLE job_application DROP COLUMN id;
ALTER TABLE job_application RENAME COLUMN new_id TO id;
ALTER TABLE job_application ADD CONSTRAINT job_application_pkey PRIMARY KEY (id);

-- 4. Repoint the dependent FK columns. CASCADE FK -> NOT NULL; SET_NULL
--    FK stays nullable.
ALTER TABLE job_application_status DROP COLUMN application_id;
ALTER TABLE job_application_status RENAME COLUMN new_application_id TO application_id;
ALTER TABLE job_application_status ALTER COLUMN application_id SET NOT NULL;
ALTER TABLE question DROP COLUMN application_id;
ALTER TABLE question RENAME COLUMN new_application_id TO application_id;

-- 5. Recreate the FKs (DEFERRABLE INITIALLY DEFERRED, matching Django) + indexes.
ALTER TABLE job_application_status
    ADD CONSTRAINT job_application_status_application_id_fk
    FOREIGN KEY (application_id) REFERENCES job_application (id)
    DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX job_application_status_application_id_idx
    ON job_application_status (application_id);
ALTER TABLE question
    ADD CONSTRAINT question_application_id_fk
    FOREIGN KEY (application_id) REFERENCES job_application (id)
    DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX question_application_id_idx ON question (application_id);
"""

SWAP_REVERSE = """
-- Reverse a destructive PK swap: NanoID PK -> a FRESH integer PK.
ALTER TABLE job_application ADD COLUMN old_int_id bigint;
ALTER TABLE job_application_status ADD COLUMN old_int_application_id bigint;
ALTER TABLE question ADD COLUMN old_int_application_id bigint;

WITH numbered AS (
    SELECT id, row_number() OVER (ORDER BY id) AS rn FROM job_application
)
UPDATE job_application j SET old_int_id = n.rn FROM numbered n WHERE j.id = n.id;

UPDATE job_application_status t SET old_int_application_id = ja.old_int_id
  FROM job_application ja WHERE t.application_id = ja.id;
UPDATE question t SET old_int_application_id = ja.old_int_id
  FROM job_application ja WHERE t.application_id = ja.id;

DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT conrelid::regclass AS tbl, conname
          FROM pg_constraint
         WHERE confrelid = 'job_application'::regclass AND contype = 'f'
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.tbl, r.conname);
    END LOOP;
END $$;
DROP INDEX IF EXISTS job_application_status_application_id_idx;
DROP INDEX IF EXISTS question_application_id_idx;

DO $$
DECLARE cname text;
BEGIN
    SELECT conname INTO cname FROM pg_constraint
     WHERE conrelid = 'job_application'::regclass AND contype = 'p';
    EXECUTE format('ALTER TABLE job_application DROP CONSTRAINT %I', cname);
END $$;
ALTER TABLE job_application DROP COLUMN id;
ALTER TABLE job_application RENAME COLUMN old_int_id TO id;
ALTER TABLE job_application ALTER COLUMN id SET NOT NULL;
ALTER TABLE job_application ADD CONSTRAINT job_application_pkey PRIMARY KEY (id);
ALTER TABLE job_application ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY;
SELECT setval(pg_get_serial_sequence('job_application', 'id'),
              (SELECT COALESCE(max(id), 1) FROM job_application));

ALTER TABLE job_application_status DROP COLUMN application_id;
ALTER TABLE job_application_status RENAME COLUMN old_int_application_id TO application_id;
ALTER TABLE job_application_status ALTER COLUMN application_id SET NOT NULL;
ALTER TABLE question DROP COLUMN application_id;
ALTER TABLE question RENAME COLUMN old_int_application_id TO application_id;

ALTER TABLE job_application_status
    ADD CONSTRAINT job_application_status_application_id_fk
    FOREIGN KEY (application_id) REFERENCES job_application (id)
    DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX job_application_status_application_id_idx
    ON job_application_status (application_id);
ALTER TABLE question
    ADD CONSTRAINT question_application_id_fk
    FOREIGN KEY (application_id) REFERENCES job_application (id)
    DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX question_application_id_idx ON question (application_id);
"""


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0120_question_nanoid_pk_swap"),
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
                    model_name="jobapplication",
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
