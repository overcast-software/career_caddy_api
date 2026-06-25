"""CC-77 #79 — Resume integer PK -> 10-char NanoID PK (true PK swap).

Copies the dependent-FK + composite-UNIQUE mechanism proven on ``Skill`` in
``0114_skill_nanoid_pk_swap`` and at fan-out on ``JobPost`` in
``0115_jobpost_nanoid_pk_swap``. Nine FKs reference ``resume(id)``:

    resume_skill.resume_id           (CASCADE,  NOT NULL)
    resume_summaries.resume_id       (CASCADE,  NOT NULL)
    resume_certification.resume_id   (CASCADE,  NOT NULL)
    resume_education.resume_id       (CASCADE,  NOT NULL)
    resume_experience.resume_id      (CASCADE,  NOT NULL)
    resume_project.resume_id         (CASCADE,  NOT NULL)
    score.resume_id                  (SET_NULL, nullable)
    cover_letter.resume_id           (SET_NULL, nullable)
    job_application.resume_id        (SET_NULL, nullable)

Resume has NO self-FK, so (unlike JobPost/Scrape) ``SET CONSTRAINTS ALL
IMMEDIATE`` is not needed — the backfill touches only staging columns, and
all dependent FKs are dropped before any column surgery.

## Constraints/indexes on resume FK columns, rebuilt on the NanoID values

    resume_skill:  UNIQUE(resume_id, skill_id)   -- unique_together(resume, skill)
    score:         UNIQUE(job_post_id, resume_id, user_id)
                   + partial UNIQUE(job_post_id, user_id) WHERE resume_id IS NULL

These carry author-given names (the resume_skill composite was last rebuilt
under ``resume_skill_resume_id_skill_id_uniq`` by 0114; the two score
constraints under their declared names by 0115), so they are dropped by
name and recreated on the NanoID columns. The partial score constraint is a
UNIQUE *index* (Django renders a ``condition=`` UniqueConstraint as
``CREATE UNIQUE INDEX ... WHERE``), so it is dropped with ``DROP INDEX`` and
recreated with ``CREATE UNIQUE INDEX`` (cf. 0115). ``score.job_post_id`` is
already a NanoID (0115) and ``resume_skill.skill_id`` already a NanoID
(0114); ``score.user_id`` stays an int (User PK swap is deferred).

Mechanism + reverse semantics: see ``0114``/``0115``.
"""

from __future__ import annotations

import logging

import job_hunting.models.nanoid_pk
from django.db import migrations, models

logger = logging.getLogger(__name__)

# (table, fk_column) for the 9 dependent FKs. All FK columns are ``resume_id``.
_DEPENDENT_FKS = [
    ("resume_skill", "resume_id"),
    ("resume_summaries", "resume_id"),
    ("resume_certification", "resume_id"),
    ("resume_education", "resume_id"),
    ("resume_experience", "resume_id"),
    ("resume_project", "resume_id"),
    ("score", "resume_id"),
    ("cover_letter", "resume_id"),
    ("job_application", "resume_id"),
]

# The six CASCADE join tables hold a required (NOT NULL) resume FK; the three
# SET_NULL relations are nullable and must stay so.
_NOT_NULL_TABLES = {
    "resume_skill",
    "resume_summaries",
    "resume_certification",
    "resume_education",
    "resume_experience",
    "resume_project",
}


def backfill_nanoids(apps, schema_editor):
    """Mint a unique NanoID per ``resume`` row, then repoint every dependent
    staging column onto the matching parent NanoID with set-based joins."""
    from job_hunting.models.nanoid_pk import generate_nanoid

    with schema_editor.connection.cursor() as cur:
        cur.execute("SELECT id FROM resume ORDER BY id")
        ids = [row[0] for row in cur.fetchall()]

        used: set[str] = set()
        for old_id in ids:
            nid = generate_nanoid()
            while nid in used:  # astronomically rare; keep it deterministic
                nid = generate_nanoid()
            used.add(nid)
            cur.execute(
                "UPDATE resume SET new_id = %s WHERE id = %s", [nid, old_id]
            )

        # Dependent FKs: one set-based UPDATE per (table, column).
        for table, col in _DEPENDENT_FKS:
            cur.execute(
                f"UPDATE {table} t SET new_{col} = r.new_id "
                f"FROM resume r WHERE t.{col} = r.id"
            )

    logger.info(
        "0123 nanoid backfill: minted %s resume ids, repointed %s dependent FKs.",
        len(ids),
        len(_DEPENDENT_FKS),
    )


# --- Staging columns ------------------------------------------------------

ADD_STAGING_COLUMNS = """
ALTER TABLE resume ADD COLUMN new_id varchar(10);
ALTER TABLE resume_skill ADD COLUMN new_resume_id varchar(10);
ALTER TABLE resume_summaries ADD COLUMN new_resume_id varchar(10);
ALTER TABLE resume_certification ADD COLUMN new_resume_id varchar(10);
ALTER TABLE resume_education ADD COLUMN new_resume_id varchar(10);
ALTER TABLE resume_experience ADD COLUMN new_resume_id varchar(10);
ALTER TABLE resume_project ADD COLUMN new_resume_id varchar(10);
ALTER TABLE score ADD COLUMN new_resume_id varchar(10);
ALTER TABLE cover_letter ADD COLUMN new_resume_id varchar(10);
ALTER TABLE job_application ADD COLUMN new_resume_id varchar(10);
"""

# Reverse of the staging-column add. The columns have already been renamed
# into place by the time this op reverses (op3's reverse runs first), so
# every drop is IF EXISTS.
DROP_STAGING_COLUMNS = """
ALTER TABLE job_application DROP COLUMN IF EXISTS new_resume_id;
ALTER TABLE cover_letter DROP COLUMN IF EXISTS new_resume_id;
ALTER TABLE score DROP COLUMN IF EXISTS new_resume_id;
ALTER TABLE resume_project DROP COLUMN IF EXISTS new_resume_id;
ALTER TABLE resume_experience DROP COLUMN IF EXISTS new_resume_id;
ALTER TABLE resume_education DROP COLUMN IF EXISTS new_resume_id;
ALTER TABLE resume_certification DROP COLUMN IF EXISTS new_resume_id;
ALTER TABLE resume_summaries DROP COLUMN IF EXISTS new_resume_id;
ALTER TABLE resume_skill DROP COLUMN IF EXISTS new_resume_id;
ALTER TABLE resume DROP COLUMN IF EXISTS new_id;
"""

# --- Forward: int PK -> NanoID PK -----------------------------------------

SWAP_FORWARD = """
-- 1. Staging PK is fully backfilled: enforce NOT NULL before promotion.
ALTER TABLE resume ALTER COLUMN new_id SET NOT NULL;

-- 2. Drop EVERY FK that references resume (the 9 dependent FKs). Django's
--    constraint names are per-environment content hashes, so resolve them
--    from the catalog. confrelid='resume' sweeps them all.
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT conrelid::regclass AS tbl, conname
          FROM pg_constraint
         WHERE confrelid = 'resume'::regclass AND contype = 'f'
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.tbl, r.conname);
    END LOOP;
END $$;

-- 3. Drop the named composite UNIQUE constraints/index that ride on a resume
--    FK column (rebuilt on the NanoID values in step 7). These carry
--    author-given names, so drop them by name.
ALTER TABLE resume_skill DROP CONSTRAINT IF EXISTS resume_skill_resume_id_skill_id_uniq;
ALTER TABLE score DROP CONSTRAINT IF EXISTS unique_score_per_job_resume_user;
DROP INDEX IF EXISTS unique_score_per_job_user_career_data;

-- 4. Promote resume's PK from the int id to the NanoID staging column.
DO $$
DECLARE cname text;
BEGIN
    SELECT conname INTO cname FROM pg_constraint
     WHERE conrelid = 'resume'::regclass AND contype = 'p';
    EXECUTE format('ALTER TABLE resume DROP CONSTRAINT %I', cname);
END $$;
ALTER TABLE resume DROP COLUMN id;
ALTER TABLE resume RENAME COLUMN new_id TO id;
ALTER TABLE resume ADD CONSTRAINT resume_pkey PRIMARY KEY (id);

-- 5. Repoint every dependent FK column. NOT NULL is restored only on the six
--    CASCADE join tables whose model FK is non-nullable; the three SET_NULL
--    relations stay nullable.
ALTER TABLE resume_skill DROP COLUMN resume_id;
ALTER TABLE resume_skill RENAME COLUMN new_resume_id TO resume_id;
ALTER TABLE resume_skill ALTER COLUMN resume_id SET NOT NULL;
ALTER TABLE resume_summaries DROP COLUMN resume_id;
ALTER TABLE resume_summaries RENAME COLUMN new_resume_id TO resume_id;
ALTER TABLE resume_summaries ALTER COLUMN resume_id SET NOT NULL;
ALTER TABLE resume_certification DROP COLUMN resume_id;
ALTER TABLE resume_certification RENAME COLUMN new_resume_id TO resume_id;
ALTER TABLE resume_certification ALTER COLUMN resume_id SET NOT NULL;
ALTER TABLE resume_education DROP COLUMN resume_id;
ALTER TABLE resume_education RENAME COLUMN new_resume_id TO resume_id;
ALTER TABLE resume_education ALTER COLUMN resume_id SET NOT NULL;
ALTER TABLE resume_experience DROP COLUMN resume_id;
ALTER TABLE resume_experience RENAME COLUMN new_resume_id TO resume_id;
ALTER TABLE resume_experience ALTER COLUMN resume_id SET NOT NULL;
ALTER TABLE resume_project DROP COLUMN resume_id;
ALTER TABLE resume_project RENAME COLUMN new_resume_id TO resume_id;
ALTER TABLE resume_project ALTER COLUMN resume_id SET NOT NULL;
ALTER TABLE score DROP COLUMN resume_id;
ALTER TABLE score RENAME COLUMN new_resume_id TO resume_id;
ALTER TABLE cover_letter DROP COLUMN resume_id;
ALTER TABLE cover_letter RENAME COLUMN new_resume_id TO resume_id;
ALTER TABLE job_application DROP COLUMN resume_id;
ALTER TABLE job_application RENAME COLUMN new_resume_id TO resume_id;

-- 6. Recreate every dependent FK (DEFERRABLE INITIALLY DEFERRED, matching
--    Django; on_delete is emulated in the ORM, never in the DB FK) + its
--    btree index.
ALTER TABLE resume_skill ADD CONSTRAINT resume_skill_resume_id_fk
    FOREIGN KEY (resume_id) REFERENCES resume (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX resume_skill_resume_id_idx ON resume_skill (resume_id);
ALTER TABLE resume_summaries ADD CONSTRAINT resume_summaries_resume_id_fk
    FOREIGN KEY (resume_id) REFERENCES resume (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX resume_summaries_resume_id_idx ON resume_summaries (resume_id);
ALTER TABLE resume_certification ADD CONSTRAINT resume_certification_resume_id_fk
    FOREIGN KEY (resume_id) REFERENCES resume (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX resume_certification_resume_id_idx ON resume_certification (resume_id);
ALTER TABLE resume_education ADD CONSTRAINT resume_education_resume_id_fk
    FOREIGN KEY (resume_id) REFERENCES resume (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX resume_education_resume_id_idx ON resume_education (resume_id);
ALTER TABLE resume_experience ADD CONSTRAINT resume_experience_resume_id_fk
    FOREIGN KEY (resume_id) REFERENCES resume (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX resume_experience_resume_id_idx ON resume_experience (resume_id);
ALTER TABLE resume_project ADD CONSTRAINT resume_project_resume_id_fk
    FOREIGN KEY (resume_id) REFERENCES resume (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX resume_project_resume_id_idx ON resume_project (resume_id);
ALTER TABLE score ADD CONSTRAINT score_resume_id_fk
    FOREIGN KEY (resume_id) REFERENCES resume (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX score_resume_id_idx ON score (resume_id);
ALTER TABLE cover_letter ADD CONSTRAINT cover_letter_resume_id_fk
    FOREIGN KEY (resume_id) REFERENCES resume (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX cover_letter_resume_id_idx ON cover_letter (resume_id);
ALTER TABLE job_application ADD CONSTRAINT job_application_resume_id_fk
    FOREIGN KEY (resume_id) REFERENCES resume (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX job_application_resume_id_idx ON job_application (resume_id);

-- 7. Recreate the composite UNIQUE constraints/index on the NanoID values.
ALTER TABLE resume_skill ADD CONSTRAINT resume_skill_resume_id_skill_id_uniq
    UNIQUE (resume_id, skill_id);
ALTER TABLE score ADD CONSTRAINT unique_score_per_job_resume_user
    UNIQUE (job_post_id, resume_id, user_id);
CREATE UNIQUE INDEX unique_score_per_job_user_career_data
    ON score (job_post_id, user_id) WHERE resume_id IS NULL;
"""

# --- Reverse: NanoID PK -> a FRESH integer PK -----------------------------

SWAP_REVERSE = """
-- 1. Integer staging columns on the parent + every FK-bearing table.
ALTER TABLE resume ADD COLUMN old_int_id bigint;
ALTER TABLE resume_skill ADD COLUMN old_int_resume_id bigint;
ALTER TABLE resume_summaries ADD COLUMN old_int_resume_id bigint;
ALTER TABLE resume_certification ADD COLUMN old_int_resume_id bigint;
ALTER TABLE resume_education ADD COLUMN old_int_resume_id bigint;
ALTER TABLE resume_experience ADD COLUMN old_int_resume_id bigint;
ALTER TABLE resume_project ADD COLUMN old_int_resume_id bigint;
ALTER TABLE score ADD COLUMN old_int_resume_id bigint;
ALTER TABLE cover_letter ADD COLUMN old_int_resume_id bigint;
ALTER TABLE job_application ADD COLUMN old_int_resume_id bigint;

-- 2. Assign fresh sequential ints (1..N) deterministically by current id.
WITH numbered AS (
    SELECT id, row_number() OVER (ORDER BY id) AS rn FROM resume
)
UPDATE resume r SET old_int_id = n.rn FROM numbered n WHERE r.id = n.id;

-- 3. Repoint dependent staging columns by joining on the NanoID.
UPDATE resume_skill t SET old_int_resume_id = r.old_int_id
  FROM resume r WHERE t.resume_id = r.id;
UPDATE resume_summaries t SET old_int_resume_id = r.old_int_id
  FROM resume r WHERE t.resume_id = r.id;
UPDATE resume_certification t SET old_int_resume_id = r.old_int_id
  FROM resume r WHERE t.resume_id = r.id;
UPDATE resume_education t SET old_int_resume_id = r.old_int_id
  FROM resume r WHERE t.resume_id = r.id;
UPDATE resume_experience t SET old_int_resume_id = r.old_int_id
  FROM resume r WHERE t.resume_id = r.id;
UPDATE resume_project t SET old_int_resume_id = r.old_int_id
  FROM resume r WHERE t.resume_id = r.id;
UPDATE score t SET old_int_resume_id = r.old_int_id
  FROM resume r WHERE t.resume_id = r.id;
UPDATE cover_letter t SET old_int_resume_id = r.old_int_id
  FROM resume r WHERE t.resume_id = r.id;
UPDATE job_application t SET old_int_resume_id = r.old_int_id
  FROM resume r WHERE t.resume_id = r.id;

-- 4. Drop all FKs referencing resume, catalog-resolved.
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT conrelid::regclass AS tbl, conname
          FROM pg_constraint
         WHERE confrelid = 'resume'::regclass AND contype = 'f'
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.tbl, r.conname);
    END LOOP;
END $$;
DROP INDEX IF EXISTS resume_skill_resume_id_idx;
DROP INDEX IF EXISTS resume_summaries_resume_id_idx;
DROP INDEX IF EXISTS resume_certification_resume_id_idx;
DROP INDEX IF EXISTS resume_education_resume_id_idx;
DROP INDEX IF EXISTS resume_experience_resume_id_idx;
DROP INDEX IF EXISTS resume_project_resume_id_idx;
DROP INDEX IF EXISTS score_resume_id_idx;
DROP INDEX IF EXISTS cover_letter_resume_id_idx;
DROP INDEX IF EXISTS job_application_resume_id_idx;

-- 5. Drop the named composite UNIQUE constraints/index (rebuilt on ints).
ALTER TABLE resume_skill DROP CONSTRAINT IF EXISTS resume_skill_resume_id_skill_id_uniq;
ALTER TABLE score DROP CONSTRAINT IF EXISTS unique_score_per_job_resume_user;
DROP INDEX IF EXISTS unique_score_per_job_user_career_data;

-- 6. Drop the NanoID PK and promote the int column to a real identity PK.
DO $$
DECLARE cname text;
BEGIN
    SELECT conname INTO cname FROM pg_constraint
     WHERE conrelid = 'resume'::regclass AND contype = 'p';
    EXECUTE format('ALTER TABLE resume DROP CONSTRAINT %I', cname);
END $$;
ALTER TABLE resume DROP COLUMN id;
ALTER TABLE resume RENAME COLUMN old_int_id TO id;
ALTER TABLE resume ALTER COLUMN id SET NOT NULL;
ALTER TABLE resume ADD CONSTRAINT resume_pkey PRIMARY KEY (id);
ALTER TABLE resume ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY;
SELECT setval(pg_get_serial_sequence('resume', 'id'),
              (SELECT COALESCE(max(id), 1) FROM resume));

-- 7. Dependent FK columns back to int (+ NOT NULL on the CASCADE join tables).
ALTER TABLE resume_skill DROP COLUMN resume_id;
ALTER TABLE resume_skill RENAME COLUMN old_int_resume_id TO resume_id;
ALTER TABLE resume_skill ALTER COLUMN resume_id SET NOT NULL;
ALTER TABLE resume_summaries DROP COLUMN resume_id;
ALTER TABLE resume_summaries RENAME COLUMN old_int_resume_id TO resume_id;
ALTER TABLE resume_summaries ALTER COLUMN resume_id SET NOT NULL;
ALTER TABLE resume_certification DROP COLUMN resume_id;
ALTER TABLE resume_certification RENAME COLUMN old_int_resume_id TO resume_id;
ALTER TABLE resume_certification ALTER COLUMN resume_id SET NOT NULL;
ALTER TABLE resume_education DROP COLUMN resume_id;
ALTER TABLE resume_education RENAME COLUMN old_int_resume_id TO resume_id;
ALTER TABLE resume_education ALTER COLUMN resume_id SET NOT NULL;
ALTER TABLE resume_experience DROP COLUMN resume_id;
ALTER TABLE resume_experience RENAME COLUMN old_int_resume_id TO resume_id;
ALTER TABLE resume_experience ALTER COLUMN resume_id SET NOT NULL;
ALTER TABLE resume_project DROP COLUMN resume_id;
ALTER TABLE resume_project RENAME COLUMN old_int_resume_id TO resume_id;
ALTER TABLE resume_project ALTER COLUMN resume_id SET NOT NULL;
ALTER TABLE score DROP COLUMN resume_id;
ALTER TABLE score RENAME COLUMN old_int_resume_id TO resume_id;
ALTER TABLE cover_letter DROP COLUMN resume_id;
ALTER TABLE cover_letter RENAME COLUMN old_int_resume_id TO resume_id;
ALTER TABLE job_application DROP COLUMN resume_id;
ALTER TABLE job_application RENAME COLUMN old_int_resume_id TO resume_id;

-- 8. Recreate the dependent FKs + indexes.
ALTER TABLE resume_skill ADD CONSTRAINT resume_skill_resume_id_fk
    FOREIGN KEY (resume_id) REFERENCES resume (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX resume_skill_resume_id_idx ON resume_skill (resume_id);
ALTER TABLE resume_summaries ADD CONSTRAINT resume_summaries_resume_id_fk
    FOREIGN KEY (resume_id) REFERENCES resume (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX resume_summaries_resume_id_idx ON resume_summaries (resume_id);
ALTER TABLE resume_certification ADD CONSTRAINT resume_certification_resume_id_fk
    FOREIGN KEY (resume_id) REFERENCES resume (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX resume_certification_resume_id_idx ON resume_certification (resume_id);
ALTER TABLE resume_education ADD CONSTRAINT resume_education_resume_id_fk
    FOREIGN KEY (resume_id) REFERENCES resume (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX resume_education_resume_id_idx ON resume_education (resume_id);
ALTER TABLE resume_experience ADD CONSTRAINT resume_experience_resume_id_fk
    FOREIGN KEY (resume_id) REFERENCES resume (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX resume_experience_resume_id_idx ON resume_experience (resume_id);
ALTER TABLE resume_project ADD CONSTRAINT resume_project_resume_id_fk
    FOREIGN KEY (resume_id) REFERENCES resume (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX resume_project_resume_id_idx ON resume_project (resume_id);
ALTER TABLE score ADD CONSTRAINT score_resume_id_fk
    FOREIGN KEY (resume_id) REFERENCES resume (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX score_resume_id_idx ON score (resume_id);
ALTER TABLE cover_letter ADD CONSTRAINT cover_letter_resume_id_fk
    FOREIGN KEY (resume_id) REFERENCES resume (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX cover_letter_resume_id_idx ON cover_letter (resume_id);
ALTER TABLE job_application ADD CONSTRAINT job_application_resume_id_fk
    FOREIGN KEY (resume_id) REFERENCES resume (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX job_application_resume_id_idx ON job_application (resume_id);

-- 9. Recreate the composite UNIQUE constraints/index on the int values.
ALTER TABLE resume_skill ADD CONSTRAINT resume_skill_resume_id_skill_id_uniq
    UNIQUE (resume_id, skill_id);
ALTER TABLE score ADD CONSTRAINT unique_score_per_job_resume_user
    UNIQUE (job_post_id, resume_id, user_id);
CREATE UNIQUE INDEX unique_score_per_job_user_career_data
    ON score (job_post_id, user_id) WHERE resume_id IS NULL;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0122_scrape_nanoid_pk_swap"),
    ]

    operations = [
        # 1. Add nullable NanoID staging columns (DB-only; not in state).
        migrations.RunSQL(
            sql=ADD_STAGING_COLUMNS,
            reverse_sql=DROP_STAGING_COLUMNS,
        ),
        # 2. Backfill the staging columns with real NanoIDs + repoint FKs.
        migrations.RunPython(backfill_nanoids, migrations.RunPython.noop),
        # 3. The PK swap: hand-written DB surgery + the matching state edit.
        #    Only AlterField(resume.id) is needed in state — the FK column
        #    types follow the parent PK type in Django's project state.
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(sql=SWAP_FORWARD, reverse_sql=SWAP_REVERSE),
            ],
            state_operations=[
                migrations.AlterField(
                    model_name="resume",
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
