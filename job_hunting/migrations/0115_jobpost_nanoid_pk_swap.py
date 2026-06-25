"""CC-57 — JobPost integer PK -> 10-char NanoID PK (true PK swap).

Lead slice of CC-77. JobPost is the only **federated** model, so its new
NanoID id is simultaneously the API resource id, the
``/api/v1/job-posts/<id>`` path key, and the ActivityPub object id (BACK-93
object-deref is shipped, so the new ids dereference). Old integer object
ids are deliberately NOT preserved (early-app decision; no dual-serve).

This copies the mechanism proven on ``Skill`` in
``0114_skill_nanoid_pk_swap`` but at JobPost's full fan-out: **13** foreign
keys reference ``job_post(id)`` — 11 on dependent tables plus the two
self-FKs ``duplicate_of`` / ``reposted_from`` — and three of those tables
carry composite indexes / named UNIQUE constraints that ride on the FK
column and must be rebuilt on the NanoID values.

## FKs repointed (all DEFERRABLE INITIALLY DEFERRED, matching Django)

    score.job_post_id                          (SET_NULL, nullable)
    job_application.job_post_id                (SET_NULL, nullable)
    scrape.job_post_id                         (SET_NULL, nullable)
    cover_letter.job_post_id                   (SET_NULL, nullable)
    question.job_post_id                       (SET_NULL, nullable)
    job_post_overwrite_decision.job_post_id    (CASCADE,  NOT NULL)
    job_post_description_decision.job_post_id   (CASCADE,  NOT NULL)
    job_post_discovery.job_post_id             (CASCADE,  NOT NULL)
    duplicate_annotation.from_jp_id            (CASCADE,  NOT NULL)
    duplicate_annotation.to_jp_id              (SET_NULL, nullable)
    duplicate_annotation.previous_to_id        (SET_NULL, nullable)
    job_post.duplicate_of_id                   (self, SET_NULL, nullable)
    job_post.reposted_from_id                  (self, SET_NULL, nullable)

Federation tables (federation_activities / federation_followers /
federation_actors) reference a JobPost only by its AS2 object **URI
string**, never by an FK — so nothing is repointed there.

## Constraints/indexes on FK columns, rebuilt on the NanoID values

    score:               UNIQUE(job_post_id, resume_id, user_id)
                         + partial UNIQUE(job_post_id, user_id) WHERE resume_id IS NULL
    job_post_discovery:  UNIQUE(job_post_id, user_id)
    *_overwrite_decision / *_description_decision:  (job_post_id, created_at DESC)
    duplicate_annotation:                           (from_jp_id, set_at DESC)

The three explicit ``Meta.indexes`` composites are recreated with their
EXACT current names so Django's migration state stays in lockstep (no
phantom RenameIndex). FK btree indexes are implicit (``db_index`` on the
ForeignKey) — Django does not track their names, so they are recreated
with clean chosen names.

## Mechanism (same as 0114): add -> backfill -> promote, via
``SeparateDatabaseAndState``

DB surgery is hand-written ``RunSQL`` (a PK-type change with dependent FKs
is not cleanly auto-generated); Django's migration *state* is kept in sync
with a single ``AlterField`` on ``jobpost.id`` (the autodetector needs
nothing more — the FK column types follow the parent PK in state). Every
Django constraint name is a per-environment content hash, so drops resolve
the name from the ``pg_constraint`` catalog rather than hardcoding it; the
FK drop is a single catalog loop over ``confrelid = 'job_post'`` which
sweeps both the dependent FKs and the two self-FKs at once.

## Reverse path (down migration)

Reversing a destructive PK swap cannot restore the original integer ids
(the forward swap dropped them). The reverse re-mints a FRESH sequential
integer PK (deterministic ``row_number`` order) and repoints every FK by
NanoID join — a structurally-valid integer-PK schema with full referential
integrity, NOT a value-preserving round-trip. Verified forward -> down ->
forward applies cleanly with FK integrity intact and unrelated FKs (e.g.
job_post.company_id, job_post.created_by_id, score.user_id) untouched.
"""

from __future__ import annotations

import logging

import job_hunting.models.nanoid_pk
from django.db import migrations, models

logger = logging.getLogger(__name__)

# Every table/column the swap touches, paired with the dependent-FK fan-out.
# (table, fk_column) for the 11 dependent FKs; the two self-FKs on job_post
# are handled inline because they live on the parent table itself.
_DEPENDENT_FKS = [
    ("score", "job_post_id"),
    ("job_application", "job_post_id"),
    ("scrape", "job_post_id"),
    ("cover_letter", "job_post_id"),
    ("question", "job_post_id"),
    ("job_post_overwrite_decision", "job_post_id"),
    ("job_post_description_decision", "job_post_id"),
    ("job_post_discovery", "job_post_id"),
    ("duplicate_annotation", "from_jp_id"),
    ("duplicate_annotation", "to_jp_id"),
    ("duplicate_annotation", "previous_to_id"),
]


def backfill_nanoids(apps, schema_editor):
    """Mint a unique NanoID per ``job_post`` row, then repoint every
    dependent staging column (and the two self-FK staging columns) onto the
    matching parent NanoID with set-based joins."""
    from job_hunting.models.nanoid_pk import generate_nanoid

    with schema_editor.connection.cursor() as cur:
        cur.execute("SELECT id FROM job_post ORDER BY id")
        job_post_ids = [row[0] for row in cur.fetchall()]

        used: set[str] = set()
        for old_id in job_post_ids:
            nid = generate_nanoid()
            while nid in used:  # astronomically rare; keep it deterministic
                nid = generate_nanoid()
            used.add(nid)
            cur.execute(
                "UPDATE job_post SET new_id = %s WHERE id = %s", [nid, old_id]
            )

        # Self-FKs: copy the parent's fresh NanoID into the child's staging col.
        cur.execute(
            "UPDATE job_post c SET new_duplicate_of_id = p.new_id "
            "FROM job_post p WHERE c.duplicate_of_id = p.id"
        )
        cur.execute(
            "UPDATE job_post c SET new_reposted_from_id = p.new_id "
            "FROM job_post p WHERE c.reposted_from_id = p.id"
        )

        # Dependent FKs: one set-based UPDATE per (table, column).
        for table, col in _DEPENDENT_FKS:
            cur.execute(
                f"UPDATE {table} t SET new_{col} = jp.new_id "
                f"FROM job_post jp WHERE t.{col} = jp.id"
            )

    logger.info(
        "0115 nanoid backfill: minted %s job_post ids, repointed %s dependent FKs.",
        len(job_post_ids),
        len(_DEPENDENT_FKS),
    )


# --- Staging columns ------------------------------------------------------

ADD_STAGING_COLUMNS = """
ALTER TABLE job_post ADD COLUMN new_id varchar(10);
ALTER TABLE job_post ADD COLUMN new_duplicate_of_id varchar(10);
ALTER TABLE job_post ADD COLUMN new_reposted_from_id varchar(10);
ALTER TABLE score ADD COLUMN new_job_post_id varchar(10);
ALTER TABLE job_application ADD COLUMN new_job_post_id varchar(10);
ALTER TABLE scrape ADD COLUMN new_job_post_id varchar(10);
ALTER TABLE cover_letter ADD COLUMN new_job_post_id varchar(10);
ALTER TABLE question ADD COLUMN new_job_post_id varchar(10);
ALTER TABLE job_post_overwrite_decision ADD COLUMN new_job_post_id varchar(10);
ALTER TABLE job_post_description_decision ADD COLUMN new_job_post_id varchar(10);
ALTER TABLE job_post_discovery ADD COLUMN new_job_post_id varchar(10);
ALTER TABLE duplicate_annotation ADD COLUMN new_from_jp_id varchar(10);
ALTER TABLE duplicate_annotation ADD COLUMN new_to_jp_id varchar(10);
ALTER TABLE duplicate_annotation ADD COLUMN new_previous_to_id varchar(10);
"""

# Reverse of the staging-column add. The columns have already been renamed
# into place by the time this op reverses (op3's reverse runs first), so
# every drop is IF EXISTS.
DROP_STAGING_COLUMNS = """
ALTER TABLE duplicate_annotation DROP COLUMN IF EXISTS new_previous_to_id;
ALTER TABLE duplicate_annotation DROP COLUMN IF EXISTS new_to_jp_id;
ALTER TABLE duplicate_annotation DROP COLUMN IF EXISTS new_from_jp_id;
ALTER TABLE job_post_discovery DROP COLUMN IF EXISTS new_job_post_id;
ALTER TABLE job_post_description_decision DROP COLUMN IF EXISTS new_job_post_id;
ALTER TABLE job_post_overwrite_decision DROP COLUMN IF EXISTS new_job_post_id;
ALTER TABLE question DROP COLUMN IF EXISTS new_job_post_id;
ALTER TABLE cover_letter DROP COLUMN IF EXISTS new_job_post_id;
ALTER TABLE scrape DROP COLUMN IF EXISTS new_job_post_id;
ALTER TABLE job_application DROP COLUMN IF EXISTS new_job_post_id;
ALTER TABLE score DROP COLUMN IF EXISTS new_job_post_id;
ALTER TABLE job_post DROP COLUMN IF EXISTS new_reposted_from_id;
ALTER TABLE job_post DROP COLUMN IF EXISTS new_duplicate_of_id;
ALTER TABLE job_post DROP COLUMN IF EXISTS new_id;
"""

# --- Forward: int PK -> NanoID PK -----------------------------------------

SWAP_FORWARD = """
-- 0. Check any deferred FK triggers NOW and run the rest of this swap in
--    IMMEDIATE mode. job_post's self-FKs (duplicate_of / reposted_from) are
--    DEFERRABLE INITIALLY DEFERRED, so the backfill UPDATEs on populated
--    tables queue pending trigger events; without this, the first
--    ALTER TABLE ... DROP CONSTRAINT below fails with "cannot ALTER TABLE
--    because it has pending trigger events". Scoped to this transaction.
SET CONSTRAINTS ALL IMMEDIATE;

-- 1. Staging PK is fully backfilled: enforce NOT NULL before promotion.
ALTER TABLE job_post ALTER COLUMN new_id SET NOT NULL;

-- 2. Drop EVERY FK that references job_post (the 11 dependent FKs + the two
--    self-FKs). Django's constraint names are per-environment content
--    hashes, so resolve them from the catalog. confrelid='job_post' is the
--    single predicate that sweeps both dependent and self FKs.
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT conrelid::regclass AS tbl, conname
          FROM pg_constraint
         WHERE confrelid = 'job_post'::regclass AND contype = 'f'
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.tbl, r.conname);
    END LOOP;
END $$;

-- 3. Drop the named composite UNIQUE constraints/indexes that ride on a
--    job_post FK column (rebuilt on the NanoID values in step 9). These
--    carry author-given names, so drop them by name.
ALTER TABLE score DROP CONSTRAINT IF EXISTS unique_score_per_job_resume_user;
DROP INDEX IF EXISTS unique_score_per_job_user_career_data;
ALTER TABLE job_post_discovery DROP CONSTRAINT IF EXISTS job_post_discovery_unique_user_post;

-- 4. Promote job_post's PK from the int id to the NanoID staging column.
DO $$
DECLARE cname text;
BEGIN
    SELECT conname INTO cname FROM pg_constraint
     WHERE conrelid = 'job_post'::regclass AND contype = 'p';
    EXECUTE format('ALTER TABLE job_post DROP CONSTRAINT %I', cname);
END $$;
ALTER TABLE job_post DROP COLUMN id;
ALTER TABLE job_post RENAME COLUMN new_id TO id;
ALTER TABLE job_post ADD CONSTRAINT job_post_pkey PRIMARY KEY (id);

-- 5. Repoint job_post's own self-FK columns onto their NanoID staging
--    values (dropping the int columns cascades their now-stale btree
--    indexes; recreated in step 7).
ALTER TABLE job_post DROP COLUMN duplicate_of_id;
ALTER TABLE job_post RENAME COLUMN new_duplicate_of_id TO duplicate_of_id;
ALTER TABLE job_post DROP COLUMN reposted_from_id;
ALTER TABLE job_post RENAME COLUMN new_reposted_from_id TO reposted_from_id;

-- 6. Repoint every dependent FK column. NOT NULL is restored only on the
--    CASCADE relations whose model FK is non-nullable.
ALTER TABLE score DROP COLUMN job_post_id;
ALTER TABLE score RENAME COLUMN new_job_post_id TO job_post_id;
ALTER TABLE job_application DROP COLUMN job_post_id;
ALTER TABLE job_application RENAME COLUMN new_job_post_id TO job_post_id;
ALTER TABLE scrape DROP COLUMN job_post_id;
ALTER TABLE scrape RENAME COLUMN new_job_post_id TO job_post_id;
ALTER TABLE cover_letter DROP COLUMN job_post_id;
ALTER TABLE cover_letter RENAME COLUMN new_job_post_id TO job_post_id;
ALTER TABLE question DROP COLUMN job_post_id;
ALTER TABLE question RENAME COLUMN new_job_post_id TO job_post_id;
ALTER TABLE job_post_overwrite_decision DROP COLUMN job_post_id;
ALTER TABLE job_post_overwrite_decision RENAME COLUMN new_job_post_id TO job_post_id;
ALTER TABLE job_post_overwrite_decision ALTER COLUMN job_post_id SET NOT NULL;
ALTER TABLE job_post_description_decision DROP COLUMN job_post_id;
ALTER TABLE job_post_description_decision RENAME COLUMN new_job_post_id TO job_post_id;
ALTER TABLE job_post_description_decision ALTER COLUMN job_post_id SET NOT NULL;
ALTER TABLE job_post_discovery DROP COLUMN job_post_id;
ALTER TABLE job_post_discovery RENAME COLUMN new_job_post_id TO job_post_id;
ALTER TABLE job_post_discovery ALTER COLUMN job_post_id SET NOT NULL;
ALTER TABLE duplicate_annotation DROP COLUMN from_jp_id;
ALTER TABLE duplicate_annotation RENAME COLUMN new_from_jp_id TO from_jp_id;
ALTER TABLE duplicate_annotation ALTER COLUMN from_jp_id SET NOT NULL;
ALTER TABLE duplicate_annotation DROP COLUMN to_jp_id;
ALTER TABLE duplicate_annotation RENAME COLUMN new_to_jp_id TO to_jp_id;
ALTER TABLE duplicate_annotation DROP COLUMN previous_to_id;
ALTER TABLE duplicate_annotation RENAME COLUMN new_previous_to_id TO previous_to_id;

-- 7. Recreate the self-FKs (NanoID -> NanoID) + their btree indexes.
ALTER TABLE job_post ADD CONSTRAINT job_post_duplicate_of_id_fk
    FOREIGN KEY (duplicate_of_id) REFERENCES job_post (id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE job_post ADD CONSTRAINT job_post_reposted_from_id_fk
    FOREIGN KEY (reposted_from_id) REFERENCES job_post (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX job_post_duplicate_of_id_idx ON job_post (duplicate_of_id);
CREATE INDEX job_post_reposted_from_id_idx ON job_post (reposted_from_id);

-- 8. Recreate every dependent FK (DEFERRABLE INITIALLY DEFERRED) + its
--    btree index.
ALTER TABLE score ADD CONSTRAINT score_job_post_id_fk
    FOREIGN KEY (job_post_id) REFERENCES job_post (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX score_job_post_id_idx ON score (job_post_id);
ALTER TABLE job_application ADD CONSTRAINT job_application_job_post_id_fk
    FOREIGN KEY (job_post_id) REFERENCES job_post (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX job_application_job_post_id_idx ON job_application (job_post_id);
ALTER TABLE scrape ADD CONSTRAINT scrape_job_post_id_fk
    FOREIGN KEY (job_post_id) REFERENCES job_post (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX scrape_job_post_id_idx ON scrape (job_post_id);
ALTER TABLE cover_letter ADD CONSTRAINT cover_letter_job_post_id_fk
    FOREIGN KEY (job_post_id) REFERENCES job_post (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX cover_letter_job_post_id_idx ON cover_letter (job_post_id);
ALTER TABLE question ADD CONSTRAINT question_job_post_id_fk
    FOREIGN KEY (job_post_id) REFERENCES job_post (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX question_job_post_id_idx ON question (job_post_id);
ALTER TABLE job_post_overwrite_decision ADD CONSTRAINT job_post_overwrite_decision_job_post_id_fk
    FOREIGN KEY (job_post_id) REFERENCES job_post (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX job_post_overwrite_decision_job_post_id_idx ON job_post_overwrite_decision (job_post_id);
ALTER TABLE job_post_description_decision ADD CONSTRAINT job_post_description_decision_job_post_id_fk
    FOREIGN KEY (job_post_id) REFERENCES job_post (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX job_post_description_decision_job_post_id_idx ON job_post_description_decision (job_post_id);
ALTER TABLE job_post_discovery ADD CONSTRAINT job_post_discovery_job_post_id_fk
    FOREIGN KEY (job_post_id) REFERENCES job_post (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX job_post_discovery_job_post_id_idx ON job_post_discovery (job_post_id);
ALTER TABLE duplicate_annotation ADD CONSTRAINT duplicate_annotation_from_jp_id_fk
    FOREIGN KEY (from_jp_id) REFERENCES job_post (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX duplicate_annotation_from_jp_id_idx ON duplicate_annotation (from_jp_id);
ALTER TABLE duplicate_annotation ADD CONSTRAINT duplicate_annotation_to_jp_id_fk
    FOREIGN KEY (to_jp_id) REFERENCES job_post (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX duplicate_annotation_to_jp_id_idx ON duplicate_annotation (to_jp_id);
ALTER TABLE duplicate_annotation ADD CONSTRAINT duplicate_annotation_previous_to_id_fk
    FOREIGN KEY (previous_to_id) REFERENCES job_post (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX duplicate_annotation_previous_to_id_idx ON duplicate_annotation (previous_to_id);

-- 9. Recreate the composite Meta.indexes (EXACT current names so Django
--    state stays in lockstep) and the named UNIQUE constraints/index.
CREATE INDEX job_post_ov_job_pos_cc73c0_idx
    ON job_post_overwrite_decision (job_post_id, created_at DESC);
CREATE INDEX job_post_de_job_pos_405d8d_idx
    ON job_post_description_decision (job_post_id, created_at DESC);
CREATE INDEX duplicate_a_from_jp_f4bfe9_idx
    ON duplicate_annotation (from_jp_id, set_at DESC);
ALTER TABLE score ADD CONSTRAINT unique_score_per_job_resume_user
    UNIQUE (job_post_id, resume_id, user_id);
CREATE UNIQUE INDEX unique_score_per_job_user_career_data
    ON score (job_post_id, user_id) WHERE resume_id IS NULL;
ALTER TABLE job_post_discovery ADD CONSTRAINT job_post_discovery_unique_user_post
    UNIQUE (job_post_id, user_id);
"""

# --- Reverse: NanoID PK -> a FRESH integer PK -----------------------------

SWAP_REVERSE = """
-- 0. Run in IMMEDIATE constraint mode for this transaction — the repoint
--    UPDATEs below touch job_post, whose self-FKs are DEFERRABLE INITIALLY
--    DEFERRED, and the deferred trigger events would otherwise block the
--    ALTER TABLE ... DROP CONSTRAINT in step 4 ("pending trigger events").
SET CONSTRAINTS ALL IMMEDIATE;

-- 1. Integer staging columns on the parent + every FK-bearing table.
ALTER TABLE job_post ADD COLUMN old_int_id bigint;
ALTER TABLE job_post ADD COLUMN old_int_duplicate_of_id bigint;
ALTER TABLE job_post ADD COLUMN old_int_reposted_from_id bigint;
ALTER TABLE score ADD COLUMN old_int_job_post_id bigint;
ALTER TABLE job_application ADD COLUMN old_int_job_post_id bigint;
ALTER TABLE scrape ADD COLUMN old_int_job_post_id bigint;
ALTER TABLE cover_letter ADD COLUMN old_int_job_post_id bigint;
ALTER TABLE question ADD COLUMN old_int_job_post_id bigint;
ALTER TABLE job_post_overwrite_decision ADD COLUMN old_int_job_post_id bigint;
ALTER TABLE job_post_description_decision ADD COLUMN old_int_job_post_id bigint;
ALTER TABLE job_post_discovery ADD COLUMN old_int_job_post_id bigint;
ALTER TABLE duplicate_annotation ADD COLUMN old_int_from_jp_id bigint;
ALTER TABLE duplicate_annotation ADD COLUMN old_int_to_jp_id bigint;
ALTER TABLE duplicate_annotation ADD COLUMN old_int_previous_to_id bigint;

-- 2. Assign fresh sequential ints (1..N) deterministically by current id.
WITH numbered AS (
    SELECT id, row_number() OVER (ORDER BY id) AS rn FROM job_post
)
UPDATE job_post j SET old_int_id = n.rn FROM numbered n WHERE j.id = n.id;

-- 3. Repoint self-FK + dependent staging columns by joining on the NanoID.
UPDATE job_post c SET old_int_duplicate_of_id = p.old_int_id
  FROM job_post p WHERE c.duplicate_of_id = p.id;
UPDATE job_post c SET old_int_reposted_from_id = p.old_int_id
  FROM job_post p WHERE c.reposted_from_id = p.id;
UPDATE score t SET old_int_job_post_id = jp.old_int_id
  FROM job_post jp WHERE t.job_post_id = jp.id;
UPDATE job_application t SET old_int_job_post_id = jp.old_int_id
  FROM job_post jp WHERE t.job_post_id = jp.id;
UPDATE scrape t SET old_int_job_post_id = jp.old_int_id
  FROM job_post jp WHERE t.job_post_id = jp.id;
UPDATE cover_letter t SET old_int_job_post_id = jp.old_int_id
  FROM job_post jp WHERE t.job_post_id = jp.id;
UPDATE question t SET old_int_job_post_id = jp.old_int_id
  FROM job_post jp WHERE t.job_post_id = jp.id;
UPDATE job_post_overwrite_decision t SET old_int_job_post_id = jp.old_int_id
  FROM job_post jp WHERE t.job_post_id = jp.id;
UPDATE job_post_description_decision t SET old_int_job_post_id = jp.old_int_id
  FROM job_post jp WHERE t.job_post_id = jp.id;
UPDATE job_post_discovery t SET old_int_job_post_id = jp.old_int_id
  FROM job_post jp WHERE t.job_post_id = jp.id;
UPDATE duplicate_annotation t SET old_int_from_jp_id = jp.old_int_id
  FROM job_post jp WHERE t.from_jp_id = jp.id;
UPDATE duplicate_annotation t SET old_int_to_jp_id = jp.old_int_id
  FROM job_post jp WHERE t.to_jp_id = jp.id;
UPDATE duplicate_annotation t SET old_int_previous_to_id = jp.old_int_id
  FROM job_post jp WHERE t.previous_to_id = jp.id;

-- 4. Drop all FKs referencing job_post (dependent + self), catalog-resolved.
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT conrelid::regclass AS tbl, conname
          FROM pg_constraint
         WHERE confrelid = 'job_post'::regclass AND contype = 'f'
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.tbl, r.conname);
    END LOOP;
END $$;

-- 5. Drop the named composite UNIQUE constraints/indexes (rebuilt on ints).
ALTER TABLE score DROP CONSTRAINT IF EXISTS unique_score_per_job_resume_user;
DROP INDEX IF EXISTS unique_score_per_job_user_career_data;
ALTER TABLE job_post_discovery DROP CONSTRAINT IF EXISTS job_post_discovery_unique_user_post;

-- 6. Drop the NanoID PK and promote the int column to a real identity PK.
DO $$
DECLARE cname text;
BEGIN
    SELECT conname INTO cname FROM pg_constraint
     WHERE conrelid = 'job_post'::regclass AND contype = 'p';
    EXECUTE format('ALTER TABLE job_post DROP CONSTRAINT %I', cname);
END $$;
ALTER TABLE job_post DROP COLUMN id;
ALTER TABLE job_post RENAME COLUMN old_int_id TO id;
ALTER TABLE job_post ALTER COLUMN id SET NOT NULL;
ALTER TABLE job_post ADD CONSTRAINT job_post_pkey PRIMARY KEY (id);
ALTER TABLE job_post ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY;
SELECT setval(pg_get_serial_sequence('job_post', 'id'),
              (SELECT COALESCE(max(id), 1) FROM job_post));

-- 7. Self-FK columns back to int.
ALTER TABLE job_post DROP COLUMN duplicate_of_id;
ALTER TABLE job_post RENAME COLUMN old_int_duplicate_of_id TO duplicate_of_id;
ALTER TABLE job_post DROP COLUMN reposted_from_id;
ALTER TABLE job_post RENAME COLUMN old_int_reposted_from_id TO reposted_from_id;

-- 8. Dependent FK columns back to int (+ NOT NULL on the CASCADE relations).
ALTER TABLE score DROP COLUMN job_post_id;
ALTER TABLE score RENAME COLUMN old_int_job_post_id TO job_post_id;
ALTER TABLE job_application DROP COLUMN job_post_id;
ALTER TABLE job_application RENAME COLUMN old_int_job_post_id TO job_post_id;
ALTER TABLE scrape DROP COLUMN job_post_id;
ALTER TABLE scrape RENAME COLUMN old_int_job_post_id TO job_post_id;
ALTER TABLE cover_letter DROP COLUMN job_post_id;
ALTER TABLE cover_letter RENAME COLUMN old_int_job_post_id TO job_post_id;
ALTER TABLE question DROP COLUMN job_post_id;
ALTER TABLE question RENAME COLUMN old_int_job_post_id TO job_post_id;
ALTER TABLE job_post_overwrite_decision DROP COLUMN job_post_id;
ALTER TABLE job_post_overwrite_decision RENAME COLUMN old_int_job_post_id TO job_post_id;
ALTER TABLE job_post_overwrite_decision ALTER COLUMN job_post_id SET NOT NULL;
ALTER TABLE job_post_description_decision DROP COLUMN job_post_id;
ALTER TABLE job_post_description_decision RENAME COLUMN old_int_job_post_id TO job_post_id;
ALTER TABLE job_post_description_decision ALTER COLUMN job_post_id SET NOT NULL;
ALTER TABLE job_post_discovery DROP COLUMN job_post_id;
ALTER TABLE job_post_discovery RENAME COLUMN old_int_job_post_id TO job_post_id;
ALTER TABLE job_post_discovery ALTER COLUMN job_post_id SET NOT NULL;
ALTER TABLE duplicate_annotation DROP COLUMN from_jp_id;
ALTER TABLE duplicate_annotation RENAME COLUMN old_int_from_jp_id TO from_jp_id;
ALTER TABLE duplicate_annotation ALTER COLUMN from_jp_id SET NOT NULL;
ALTER TABLE duplicate_annotation DROP COLUMN to_jp_id;
ALTER TABLE duplicate_annotation RENAME COLUMN old_int_to_jp_id TO to_jp_id;
ALTER TABLE duplicate_annotation DROP COLUMN previous_to_id;
ALTER TABLE duplicate_annotation RENAME COLUMN old_int_previous_to_id TO previous_to_id;

-- 9. Recreate the self-FKs + indexes.
ALTER TABLE job_post ADD CONSTRAINT job_post_duplicate_of_id_fk
    FOREIGN KEY (duplicate_of_id) REFERENCES job_post (id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE job_post ADD CONSTRAINT job_post_reposted_from_id_fk
    FOREIGN KEY (reposted_from_id) REFERENCES job_post (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX job_post_duplicate_of_id_idx ON job_post (duplicate_of_id);
CREATE INDEX job_post_reposted_from_id_idx ON job_post (reposted_from_id);

-- 10. Recreate the dependent FKs + indexes.
ALTER TABLE score ADD CONSTRAINT score_job_post_id_fk
    FOREIGN KEY (job_post_id) REFERENCES job_post (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX score_job_post_id_idx ON score (job_post_id);
ALTER TABLE job_application ADD CONSTRAINT job_application_job_post_id_fk
    FOREIGN KEY (job_post_id) REFERENCES job_post (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX job_application_job_post_id_idx ON job_application (job_post_id);
ALTER TABLE scrape ADD CONSTRAINT scrape_job_post_id_fk
    FOREIGN KEY (job_post_id) REFERENCES job_post (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX scrape_job_post_id_idx ON scrape (job_post_id);
ALTER TABLE cover_letter ADD CONSTRAINT cover_letter_job_post_id_fk
    FOREIGN KEY (job_post_id) REFERENCES job_post (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX cover_letter_job_post_id_idx ON cover_letter (job_post_id);
ALTER TABLE question ADD CONSTRAINT question_job_post_id_fk
    FOREIGN KEY (job_post_id) REFERENCES job_post (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX question_job_post_id_idx ON question (job_post_id);
ALTER TABLE job_post_overwrite_decision ADD CONSTRAINT job_post_overwrite_decision_job_post_id_fk
    FOREIGN KEY (job_post_id) REFERENCES job_post (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX job_post_overwrite_decision_job_post_id_idx ON job_post_overwrite_decision (job_post_id);
ALTER TABLE job_post_description_decision ADD CONSTRAINT job_post_description_decision_job_post_id_fk
    FOREIGN KEY (job_post_id) REFERENCES job_post (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX job_post_description_decision_job_post_id_idx ON job_post_description_decision (job_post_id);
ALTER TABLE job_post_discovery ADD CONSTRAINT job_post_discovery_job_post_id_fk
    FOREIGN KEY (job_post_id) REFERENCES job_post (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX job_post_discovery_job_post_id_idx ON job_post_discovery (job_post_id);
ALTER TABLE duplicate_annotation ADD CONSTRAINT duplicate_annotation_from_jp_id_fk
    FOREIGN KEY (from_jp_id) REFERENCES job_post (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX duplicate_annotation_from_jp_id_idx ON duplicate_annotation (from_jp_id);
ALTER TABLE duplicate_annotation ADD CONSTRAINT duplicate_annotation_to_jp_id_fk
    FOREIGN KEY (to_jp_id) REFERENCES job_post (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX duplicate_annotation_to_jp_id_idx ON duplicate_annotation (to_jp_id);
ALTER TABLE duplicate_annotation ADD CONSTRAINT duplicate_annotation_previous_to_id_fk
    FOREIGN KEY (previous_to_id) REFERENCES job_post (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX duplicate_annotation_previous_to_id_idx ON duplicate_annotation (previous_to_id);

-- 11. Recreate the composite Meta.indexes + named UNIQUE constraints/index.
CREATE INDEX job_post_ov_job_pos_cc73c0_idx
    ON job_post_overwrite_decision (job_post_id, created_at DESC);
CREATE INDEX job_post_de_job_pos_405d8d_idx
    ON job_post_description_decision (job_post_id, created_at DESC);
CREATE INDEX duplicate_a_from_jp_f4bfe9_idx
    ON duplicate_annotation (from_jp_id, set_at DESC);
ALTER TABLE score ADD CONSTRAINT unique_score_per_job_resume_user
    UNIQUE (job_post_id, resume_id, user_id);
CREATE UNIQUE INDEX unique_score_per_job_user_career_data
    ON score (job_post_id, user_id) WHERE resume_id IS NULL;
ALTER TABLE job_post_discovery ADD CONSTRAINT job_post_discovery_unique_user_post
    UNIQUE (job_post_id, user_id);
"""


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0114_skill_nanoid_pk_swap"),
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
        #    Only AlterField(jobpost.id) is needed in state — the FK column
        #    types follow the parent PK type in Django's project state.
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(sql=SWAP_FORWARD, reverse_sql=SWAP_REVERSE),
            ],
            state_operations=[
                migrations.AlterField(
                    model_name="jobpost",
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
