"""CC-78 — TEMPLATE migration: swap an integer PK to a NanoID PK with a
dependent FK, in Postgres + Django.

This is the reference mechanism the JobPost slice (#57) and the
remaining-models rollout (#79) copy. The proof model is ``Skill`` —
lowest-risk swap that still exercises every hard part:

  * a populated integer PK (``skill.id``, identity/bigint),
  * a dependent FK on another table (``resume_skill.skill_id``),
  * a composite UNIQUE that includes the FK column
    (``unique_together(resume, skill)``), which must be dropped and
    recreated, and
  * a FK btree index that rides on the FK column.

A PK-type change with dependent FKs is NOT cleanly auto-generated, so the
DB surgery is hand-written ``RunSQL`` while Django's migration *state* is
kept in sync via ``SeparateDatabaseAndState`` (the ``AlterField`` ops).
That split is what keeps ``makemigrations`` clean afterwards and lets
later migrations build on the new field types.

## Mechanism (approach "a": add → backfill → repoint → promote)

  1. Add nullable NanoID staging columns (``skill.new_id``,
     ``resume_skill.new_skill_id``).
  2. Backfill: mint a unique NanoID per skill row with the SAME
     ``generate_nanoid`` callable the model default uses, then repoint the
     dependent staging column with one set-based UPDATE.
  3. Promote, inside one atomic transaction (Postgres DDL is
     transactional, so the whole swap is all-or-nothing):
       - drop the dependent FK (resolved from the catalog — its Django
         name is a per-environment content hash, never hardcoded),
       - drop the composite UNIQUE that references the int FK column,
       - drop the int PK, drop the int ``id``, rename the NanoID column
         into place, add the new PK,
       - repoint the FK column, recreate the FK + composite UNIQUE + FK
         index on the NanoID values.

## Reverse path (down migration)

Reversing a *destructive* PK swap cannot restore the original integer ids
(the forward swap dropped them). The reverse therefore re-mints a FRESH
sequential integer PK (deterministic order) and repoints the FK by NanoID
join — yielding a structurally-valid integer-PK schema with full
referential integrity. That is the correct semantics for reversing a
destructive swap, NOT a value-preserving round-trip. Verified: forward →
down → forward all apply cleanly.
"""

from __future__ import annotations

import logging

import job_hunting.models.nanoid_pk
from django.db import migrations, models


def backfill_nanoids(apps, schema_editor):
    """Mint a unique NanoID for every ``skill`` row, then repoint the
    dependent ``resume_skill`` staging column to match."""
    logger = logging.getLogger(__name__)
    from job_hunting.models.nanoid_pk import generate_nanoid

    with schema_editor.connection.cursor() as cur:
        cur.execute("SELECT id FROM skill ORDER BY id")
        skill_ids = [row[0] for row in cur.fetchall()]

        used: set[str] = set()
        for old_id in skill_ids:
            nid = generate_nanoid()
            while nid in used:  # astronomically rare; keep it deterministic
                nid = generate_nanoid()
            used.add(nid)
            cur.execute(
                "UPDATE skill SET new_id = %s WHERE id = %s", [nid, old_id]
            )

        # Set-based FK repoint: copy each row's parent NanoID into the
        # dependent staging column in one statement.
        cur.execute(
            "UPDATE resume_skill rs SET new_skill_id = s.new_id "
            "FROM skill s WHERE rs.skill_id = s.id"
        )

    logger.info(
        "0114 nanoid backfill: minted %s skill ids, repointed resume_skill.",
        len(skill_ids),
    )


# --- DB surgery -----------------------------------------------------------

ADD_STAGING_COLUMNS = """
ALTER TABLE skill ADD COLUMN new_id varchar(10);
ALTER TABLE resume_skill ADD COLUMN new_skill_id varchar(10);
"""

DROP_STAGING_COLUMNS = """
ALTER TABLE resume_skill DROP COLUMN IF EXISTS new_skill_id;
ALTER TABLE skill DROP COLUMN IF EXISTS new_id;
"""

SWAP_FORWARD = """
-- 1. Staging column is fully backfilled: enforce NOT NULL pre-promotion.
ALTER TABLE skill ALTER COLUMN new_id SET NOT NULL;

-- 2. Drop the dependent FK resume_skill.skill_id -> skill.id. Django's
--    constraint name is a per-environment content hash, so resolve it
--    from the catalog rather than hardcoding it.
DO $$
DECLARE cname text;
BEGIN
    SELECT conname INTO cname
      FROM pg_constraint
     WHERE conrelid = 'resume_skill'::regclass
       AND contype  = 'f'
       AND confrelid = 'skill'::regclass;
    IF cname IS NOT NULL THEN
        EXECUTE format('ALTER TABLE resume_skill DROP CONSTRAINT %I', cname);
    END IF;
END $$;

-- 3. Drop every composite UNIQUE on resume_skill (the
--    unique_together(resume, skill)) — it references the int skill_id we
--    are about to drop. Recreated on the NanoID column in step 7.
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT conname FROM pg_constraint
         WHERE conrelid = 'resume_skill'::regclass AND contype = 'u'
    LOOP
        EXECUTE format('ALTER TABLE resume_skill DROP CONSTRAINT %I', r.conname);
    END LOOP;
END $$;

-- 4. Promote skill's PK from the int id to the NanoID staging column.
DO $$
DECLARE cname text;
BEGIN
    SELECT conname INTO cname FROM pg_constraint
     WHERE conrelid = 'skill'::regclass AND contype = 'p';
    EXECUTE format('ALTER TABLE skill DROP CONSTRAINT %I', cname);
END $$;
ALTER TABLE skill DROP COLUMN id;
ALTER TABLE skill RENAME COLUMN new_id TO id;
ALTER TABLE skill ADD CONSTRAINT skill_pkey PRIMARY KEY (id);

-- 5. Repoint the dependent FK column to its backfilled NanoID values.
ALTER TABLE resume_skill DROP COLUMN skill_id;
ALTER TABLE resume_skill RENAME COLUMN new_skill_id TO skill_id;
ALTER TABLE resume_skill ALTER COLUMN skill_id SET NOT NULL;

-- 6. Recreate the FK (NanoID -> NanoID), matching Django's deferred-FK
--    default so runtime behaviour is identical to an ORM-built constraint.
ALTER TABLE resume_skill
    ADD CONSTRAINT resume_skill_skill_id_fk
    FOREIGN KEY (skill_id) REFERENCES skill (id)
    DEFERRABLE INITIALLY DEFERRED;

-- 7. Recreate the composite UNIQUE(resume_id, skill_id).
ALTER TABLE resume_skill
    ADD CONSTRAINT resume_skill_resume_id_skill_id_uniq
    UNIQUE (resume_id, skill_id);

-- 8. Recreate the FK btree index (a ForeignKey carries db_index=True; the
--    int column's index was dropped with the column).
CREATE INDEX resume_skill_skill_id_idx ON resume_skill (skill_id);
"""

SWAP_REVERSE = """
-- Reverse a destructive PK swap: NanoID PK -> a FRESH integer PK. The
-- original ints were dropped by the forward swap and cannot be restored,
-- so re-sequence deterministically by current id. Result: a
-- structurally-valid integer-PK schema with full FK integrity.

-- 1. Integer staging columns.
ALTER TABLE skill ADD COLUMN old_int_id bigint;
ALTER TABLE resume_skill ADD COLUMN old_int_skill_id bigint;

-- 2. Assign fresh sequential ints (1..N) in a deterministic order.
WITH numbered AS (
    SELECT id, row_number() OVER (ORDER BY id) AS rn FROM skill
)
UPDATE skill s SET old_int_id = n.rn FROM numbered n WHERE s.id = n.id;

-- 3. Repoint the FK staging column by joining on the NanoID.
UPDATE resume_skill rs
   SET old_int_skill_id = s.old_int_id
  FROM skill s
 WHERE rs.skill_id = s.id;

-- 4. Drop ONLY the skill-referencing FK (mirror of the forward path's
--    targeted drop — leave resume_skill's other FKs, e.g. resume_id,
--    intact), plus the composite UNIQUE and the FK index.
DO $$
DECLARE cname text;
BEGIN
    SELECT conname INTO cname
      FROM pg_constraint
     WHERE conrelid = 'resume_skill'::regclass
       AND contype  = 'f'
       AND confrelid = 'skill'::regclass;
    IF cname IS NOT NULL THEN
        EXECUTE format('ALTER TABLE resume_skill DROP CONSTRAINT %I', cname);
    END IF;
END $$;
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT conname FROM pg_constraint
         WHERE conrelid = 'resume_skill'::regclass AND contype = 'u'
    LOOP
        EXECUTE format('ALTER TABLE resume_skill DROP CONSTRAINT %I', r.conname);
    END LOOP;
END $$;
DROP INDEX IF EXISTS resume_skill_skill_id_idx;

-- 5. Drop the NanoID PK and promote the integer column to a real
--    identity PK (matches BigAutoField semantics for future inserts).
DO $$
DECLARE cname text;
BEGIN
    SELECT conname INTO cname FROM pg_constraint
     WHERE conrelid = 'skill'::regclass AND contype = 'p';
    EXECUTE format('ALTER TABLE skill DROP CONSTRAINT %I', cname);
END $$;
ALTER TABLE skill DROP COLUMN id;
ALTER TABLE skill RENAME COLUMN old_int_id TO id;
ALTER TABLE skill ALTER COLUMN id SET NOT NULL;
ALTER TABLE skill ADD CONSTRAINT skill_pkey PRIMARY KEY (id);
ALTER TABLE skill ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY;
SELECT setval(pg_get_serial_sequence('skill', 'id'),
              (SELECT COALESCE(max(id), 1) FROM skill));

-- 6. Promote resume_skill's integer FK and recreate its constraints.
ALTER TABLE resume_skill DROP COLUMN skill_id;
ALTER TABLE resume_skill RENAME COLUMN old_int_skill_id TO skill_id;
ALTER TABLE resume_skill ALTER COLUMN skill_id SET NOT NULL;
ALTER TABLE resume_skill
    ADD CONSTRAINT resume_skill_skill_id_fk
    FOREIGN KEY (skill_id) REFERENCES skill (id)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE resume_skill
    ADD CONSTRAINT resume_skill_resume_id_skill_id_uniq
    UNIQUE (resume_id, skill_id);
CREATE INDEX resume_skill_skill_id_idx ON resume_skill (skill_id);
"""


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0113_register_stale_unclaimed_hold_sweep_schedule"),
    ]

    operations = [
        # 1. Add nullable NanoID staging columns (DB-only; not in state).
        migrations.RunSQL(
            sql=ADD_STAGING_COLUMNS,
            reverse_sql=DROP_STAGING_COLUMNS,
        ),
        # 2. Backfill the staging columns with real NanoIDs.
        migrations.RunPython(backfill_nanoids, migrations.RunPython.noop),
        # 3. The PK swap: hand-written DB surgery + matching state edits.
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(sql=SWAP_FORWARD, reverse_sql=SWAP_REVERSE),
            ],
            state_operations=[
                migrations.AlterField(
                    model_name="skill",
                    name="id",
                    field=models.CharField(
                        default=job_hunting.models.nanoid_pk.generate_nanoid,
                        editable=False,
                        max_length=10,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                migrations.AlterField(
                    model_name="resumeskill",
                    name="skill",
                    field=models.ForeignKey(
                        db_column="skill_id",
                        on_delete=models.deletion.CASCADE,
                        related_name="resume_skills",
                        to="job_hunting.skill",
                    ),
                ),
            ],
        ),
    ]
