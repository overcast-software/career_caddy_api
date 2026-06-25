"""CC-77 #79 — CoverLetter integer PK -> 10-char NanoID PK (true PK swap).

Copies the dependent-FK mechanism proven on ``Skill`` in
``0114_skill_nanoid_pk_swap``. One FK references ``cover_letter(id)``:

    job_application.cover_letter_id   (SET_NULL, nullable)

The FK column is nullable (``on_delete=SET_NULL``), so it is repointed
WITHOUT a ``SET NOT NULL`` — NULL rows stay NULL. No composite
constraint/index rides on ``cover_letter_id`` (JobApplication declares no
``Meta.indexes``/``constraints``), so only the implicit FK btree index is
recreated (clean chosen name — Django does not track FK index names).

Mechanism + reverse semantics: see ``0114``/``0116``. DB surgery is
hand-written ``RunSQL``; state is kept in sync with a single
``AlterField`` on ``coverletter.id`` via ``SeparateDatabaseAndState`` (the
FK column type follows the parent PK type in Django state).
"""

from __future__ import annotations

import logging

import job_hunting.models.nanoid_pk
from django.db import migrations, models

logger = logging.getLogger(__name__)


def backfill_nanoids(apps, schema_editor):
    """Mint a unique NanoID per ``cover_letter`` row, then repoint the
    dependent ``job_application.cover_letter_id`` staging column."""
    from job_hunting.models.nanoid_pk import generate_nanoid

    with schema_editor.connection.cursor() as cur:
        cur.execute("SELECT id FROM cover_letter ORDER BY id")
        ids = [row[0] for row in cur.fetchall()]

        used: set[str] = set()
        for old_id in ids:
            nid = generate_nanoid()
            while nid in used:  # astronomically rare; keep it deterministic
                nid = generate_nanoid()
            used.add(nid)
            cur.execute(
                "UPDATE cover_letter SET new_id = %s WHERE id = %s",
                [nid, old_id],
            )

        cur.execute(
            "UPDATE job_application ja SET new_cover_letter_id = cl.new_id "
            "FROM cover_letter cl WHERE ja.cover_letter_id = cl.id"
        )

    logger.info("0119 nanoid backfill: minted %s cover_letter ids.", len(ids))


ADD_STAGING_COLUMNS = """
ALTER TABLE cover_letter ADD COLUMN new_id varchar(10);
ALTER TABLE job_application ADD COLUMN new_cover_letter_id varchar(10);
"""

DROP_STAGING_COLUMNS = """
ALTER TABLE job_application DROP COLUMN IF EXISTS new_cover_letter_id;
ALTER TABLE cover_letter DROP COLUMN IF EXISTS new_id;
"""

SWAP_FORWARD = """
-- 1. Staging PK is fully backfilled: enforce NOT NULL before promotion.
ALTER TABLE cover_letter ALTER COLUMN new_id SET NOT NULL;

-- 2. Drop the dependent FK job_application.cover_letter_id -> cover_letter.id
--    (catalog-resolved name; confrelid filter targets only this FK).
DO $$
DECLARE cname text;
BEGIN
    SELECT conname INTO cname
      FROM pg_constraint
     WHERE conrelid = 'job_application'::regclass
       AND contype  = 'f'
       AND confrelid = 'cover_letter'::regclass;
    IF cname IS NOT NULL THEN
        EXECUTE format('ALTER TABLE job_application DROP CONSTRAINT %I', cname);
    END IF;
END $$;

-- 3. Promote cover_letter's PK from the int id to the NanoID staging column.
DO $$
DECLARE cname text;
BEGIN
    SELECT conname INTO cname FROM pg_constraint
     WHERE conrelid = 'cover_letter'::regclass AND contype = 'p';
    EXECUTE format('ALTER TABLE cover_letter DROP CONSTRAINT %I', cname);
END $$;
ALTER TABLE cover_letter DROP COLUMN id;
ALTER TABLE cover_letter RENAME COLUMN new_id TO id;
ALTER TABLE cover_letter ADD CONSTRAINT cover_letter_pkey PRIMARY KEY (id);

-- 4. Repoint the dependent FK column (nullable: no SET NOT NULL).
ALTER TABLE job_application DROP COLUMN cover_letter_id;
ALTER TABLE job_application RENAME COLUMN new_cover_letter_id TO cover_letter_id;

-- 5. Recreate the FK (DEFERRABLE INITIALLY DEFERRED, matching Django) + index.
ALTER TABLE job_application
    ADD CONSTRAINT job_application_cover_letter_id_fk
    FOREIGN KEY (cover_letter_id) REFERENCES cover_letter (id)
    DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX job_application_cover_letter_id_idx
    ON job_application (cover_letter_id);
"""

SWAP_REVERSE = """
-- Reverse a destructive PK swap: NanoID PK -> a FRESH integer PK.
ALTER TABLE cover_letter ADD COLUMN old_int_id bigint;
ALTER TABLE job_application ADD COLUMN old_int_cover_letter_id bigint;

WITH numbered AS (
    SELECT id, row_number() OVER (ORDER BY id) AS rn FROM cover_letter
)
UPDATE cover_letter c SET old_int_id = n.rn FROM numbered n WHERE c.id = n.id;

UPDATE job_application ja SET old_int_cover_letter_id = cl.old_int_id
  FROM cover_letter cl WHERE ja.cover_letter_id = cl.id;

DO $$
DECLARE cname text;
BEGIN
    SELECT conname INTO cname
      FROM pg_constraint
     WHERE conrelid = 'job_application'::regclass
       AND contype  = 'f'
       AND confrelid = 'cover_letter'::regclass;
    IF cname IS NOT NULL THEN
        EXECUTE format('ALTER TABLE job_application DROP CONSTRAINT %I', cname);
    END IF;
END $$;
DROP INDEX IF EXISTS job_application_cover_letter_id_idx;

DO $$
DECLARE cname text;
BEGIN
    SELECT conname INTO cname FROM pg_constraint
     WHERE conrelid = 'cover_letter'::regclass AND contype = 'p';
    EXECUTE format('ALTER TABLE cover_letter DROP CONSTRAINT %I', cname);
END $$;
ALTER TABLE cover_letter DROP COLUMN id;
ALTER TABLE cover_letter RENAME COLUMN old_int_id TO id;
ALTER TABLE cover_letter ALTER COLUMN id SET NOT NULL;
ALTER TABLE cover_letter ADD CONSTRAINT cover_letter_pkey PRIMARY KEY (id);
ALTER TABLE cover_letter ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY;
SELECT setval(pg_get_serial_sequence('cover_letter', 'id'),
              (SELECT COALESCE(max(id), 1) FROM cover_letter));

ALTER TABLE job_application DROP COLUMN cover_letter_id;
ALTER TABLE job_application RENAME COLUMN old_int_cover_letter_id TO cover_letter_id;
ALTER TABLE job_application
    ADD CONSTRAINT job_application_cover_letter_id_fk
    FOREIGN KEY (cover_letter_id) REFERENCES cover_letter (id)
    DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX job_application_cover_letter_id_idx
    ON job_application (cover_letter_id);
"""


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0118_answer_nanoid_pk_swap"),
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
                    model_name="coverletter",
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
