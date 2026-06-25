"""CC-77 #79 — Company integer PK -> 10-char NanoID PK (true PK swap).

Copies the self-FK fan-out mechanism proven on ``JobPost`` in
``0115_jobpost_nanoid_pk_swap``. Nine external FKs reference ``company(id)``
plus the self-FK ``company.canonical_id``:

    federation_actors.company_id     (CASCADE,  nullable)
    experience.company_id            (SET_NULL, nullable)
    question.company_id              (SET_NULL, nullable)
    job_application.company_id       (SET_NULL, nullable)
    federation_followers.company_id  (CASCADE,  nullable)
    scrape.company_id                (SET_NULL, nullable)
    company_alias.company_id         (CASCADE,  NOT NULL)
    job_post.company_id              (SET_NULL, nullable)
    cover_letter.company_id          (SET_NULL, nullable)
    company.canonical_id             (self, SET_NULL, nullable)

The self-FK makes ``SET CONSTRAINTS ALL IMMEDIATE`` necessary (cf. 0115).

## Constraints/indexes on company columns, rebuilt on the NanoID values

    company:               CheckConstraint company_canonical_not_self
                           (canonical_id IS NULL OR canonical_id <> id) — rides
                           on BOTH id and canonical_id, so it is dropped before
                           the PK/self-FK surgery and recreated after.
    federation_actors:     CheckConstraint actor_user_company_mutually_exclusive
                           (user_id IS NULL OR company_id IS NULL).
    federation_followers:  CheckConstraint federation_follower_followee_required
                           (local_user_id IS NOT NULL OR company_id IS NOT NULL)
                           + partial UNIQUE federation_follower_unique_company_remote
                           (company_id, actor_uri) WHERE company_id IS NOT NULL.

These carry author-given names, so they are dropped by name and recreated on
the NanoID columns. ``federation_follower_unique_local_remote`` rides on
``local_user`` (User PK, still int — deferred) NOT on company, so it is left
untouched. Company's own ``name``/``slug`` UNIQUE constraints ride on other
columns and are likewise untouched. The single-column FK / Meta ``Index(company)``
btree indexes on company_alias + federation_followers are implicit-dropped by
``DROP COLUMN company_id`` and recreated once per column with clean names (cf.
0115 — Django does not track FK index names, and makemigrations is already
ungated on these tables' phantom index renames).

Mechanism + reverse semantics: see ``0114``/``0115``.
"""

from __future__ import annotations

import logging

import job_hunting.models.nanoid_pk
from django.db import migrations, models

logger = logging.getLogger(__name__)

# (table, fk_column) for the 9 external dependent FKs; the self-FK on company
# is handled inline because it lives on the parent table itself. All FK columns
# are ``company_id``.
_DEPENDENT_FKS = [
    ("federation_actors", "company_id"),
    ("experience", "company_id"),
    ("question", "company_id"),
    ("job_application", "company_id"),
    ("federation_followers", "company_id"),
    ("scrape", "company_id"),
    ("company_alias", "company_id"),
    ("job_post", "company_id"),
    ("cover_letter", "company_id"),
]


def backfill_nanoids(apps, schema_editor):
    """Mint a unique NanoID per ``company`` row, then repoint the self-FK and
    every dependent staging column with set-based joins."""
    from job_hunting.models.nanoid_pk import generate_nanoid

    with schema_editor.connection.cursor() as cur:
        cur.execute("SELECT id FROM company ORDER BY id")
        ids = [row[0] for row in cur.fetchall()]

        used: set[str] = set()
        for old_id in ids:
            nid = generate_nanoid()
            while nid in used:  # astronomically rare; keep it deterministic
                nid = generate_nanoid()
            used.add(nid)
            cur.execute(
                "UPDATE company SET new_id = %s WHERE id = %s", [nid, old_id]
            )

        # Self-FK: copy the parent's fresh NanoID into the child staging col.
        cur.execute(
            "UPDATE company c SET new_canonical_id = p.new_id "
            "FROM company p WHERE c.canonical_id = p.id"
        )

        for table, col in _DEPENDENT_FKS:
            cur.execute(
                f"UPDATE {table} t SET new_{col} = co.new_id "
                f"FROM company co WHERE t.{col} = co.id"
            )

    logger.info(
        "0124 nanoid backfill: minted %s company ids, repointed %s dependent FKs.",
        len(ids),
        len(_DEPENDENT_FKS),
    )


# --- Staging columns ------------------------------------------------------

ADD_STAGING_COLUMNS = """
ALTER TABLE company ADD COLUMN new_id varchar(10);
ALTER TABLE company ADD COLUMN new_canonical_id varchar(10);
ALTER TABLE federation_actors ADD COLUMN new_company_id varchar(10);
ALTER TABLE experience ADD COLUMN new_company_id varchar(10);
ALTER TABLE question ADD COLUMN new_company_id varchar(10);
ALTER TABLE job_application ADD COLUMN new_company_id varchar(10);
ALTER TABLE federation_followers ADD COLUMN new_company_id varchar(10);
ALTER TABLE scrape ADD COLUMN new_company_id varchar(10);
ALTER TABLE company_alias ADD COLUMN new_company_id varchar(10);
ALTER TABLE job_post ADD COLUMN new_company_id varchar(10);
ALTER TABLE cover_letter ADD COLUMN new_company_id varchar(10);
"""

# Reverse of the staging-column add. The columns have already been renamed
# into place by the time this op reverses (op3's reverse runs first), so
# every drop is IF EXISTS.
DROP_STAGING_COLUMNS = """
ALTER TABLE cover_letter DROP COLUMN IF EXISTS new_company_id;
ALTER TABLE job_post DROP COLUMN IF EXISTS new_company_id;
ALTER TABLE company_alias DROP COLUMN IF EXISTS new_company_id;
ALTER TABLE scrape DROP COLUMN IF EXISTS new_company_id;
ALTER TABLE federation_followers DROP COLUMN IF EXISTS new_company_id;
ALTER TABLE job_application DROP COLUMN IF EXISTS new_company_id;
ALTER TABLE question DROP COLUMN IF EXISTS new_company_id;
ALTER TABLE experience DROP COLUMN IF EXISTS new_company_id;
ALTER TABLE federation_actors DROP COLUMN IF EXISTS new_company_id;
ALTER TABLE company DROP COLUMN IF EXISTS new_canonical_id;
ALTER TABLE company DROP COLUMN IF EXISTS new_id;
"""

# --- Forward: int PK -> NanoID PK -----------------------------------------

SWAP_FORWARD = """
-- 0. Check deferred FK triggers NOW and run the rest in IMMEDIATE mode —
--    company's self-FK (canonical) is DEFERRABLE INITIALLY DEFERRED, so the
--    backfill UPDATEs queue pending trigger events that would block the first
--    ALTER TABLE ... DROP CONSTRAINT. Scoped to this transaction.
SET CONSTRAINTS ALL IMMEDIATE;

-- 1. Staging PK is fully backfilled: enforce NOT NULL before promotion.
ALTER TABLE company ALTER COLUMN new_id SET NOT NULL;

-- 2. Drop EVERY FK that references company (9 dependent + the self-FK).
--    confrelid='company' is the single predicate that sweeps them all.
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT conrelid::regclass AS tbl, conname
          FROM pg_constraint
         WHERE confrelid = 'company'::regclass AND contype = 'f'
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.tbl, r.conname);
    END LOOP;
END $$;

-- 3. Drop the named CHECK / partial-UNIQUE objects that ride on a company
--    column (rebuilt on the NanoID values in step 9). company_canonical_not_self
--    references BOTH id and canonical_id, so it must go before the PK surgery.
ALTER TABLE company DROP CONSTRAINT IF EXISTS company_canonical_not_self;
ALTER TABLE federation_actors DROP CONSTRAINT IF EXISTS actor_user_company_mutually_exclusive;
ALTER TABLE federation_followers DROP CONSTRAINT IF EXISTS federation_follower_followee_required;
DROP INDEX IF EXISTS federation_follower_unique_company_remote;

-- 4. Promote company's PK from the int id to the NanoID staging column.
DO $$
DECLARE cname text;
BEGIN
    SELECT conname INTO cname FROM pg_constraint
     WHERE conrelid = 'company'::regclass AND contype = 'p';
    EXECUTE format('ALTER TABLE company DROP CONSTRAINT %I', cname);
END $$;
ALTER TABLE company DROP COLUMN id;
ALTER TABLE company RENAME COLUMN new_id TO id;
ALTER TABLE company ADD CONSTRAINT company_pkey PRIMARY KEY (id);

-- 5. Repoint company's own self-FK column (nullable: no SET NOT NULL).
ALTER TABLE company DROP COLUMN canonical_id;
ALTER TABLE company RENAME COLUMN new_canonical_id TO canonical_id;

-- 6. Repoint every dependent FK column. NOT NULL is restored only on
--    company_alias (whose model FK is non-nullable).
ALTER TABLE federation_actors DROP COLUMN company_id;
ALTER TABLE federation_actors RENAME COLUMN new_company_id TO company_id;
ALTER TABLE experience DROP COLUMN company_id;
ALTER TABLE experience RENAME COLUMN new_company_id TO company_id;
ALTER TABLE question DROP COLUMN company_id;
ALTER TABLE question RENAME COLUMN new_company_id TO company_id;
ALTER TABLE job_application DROP COLUMN company_id;
ALTER TABLE job_application RENAME COLUMN new_company_id TO company_id;
ALTER TABLE federation_followers DROP COLUMN company_id;
ALTER TABLE federation_followers RENAME COLUMN new_company_id TO company_id;
ALTER TABLE scrape DROP COLUMN company_id;
ALTER TABLE scrape RENAME COLUMN new_company_id TO company_id;
ALTER TABLE company_alias DROP COLUMN company_id;
ALTER TABLE company_alias RENAME COLUMN new_company_id TO company_id;
ALTER TABLE company_alias ALTER COLUMN company_id SET NOT NULL;
ALTER TABLE job_post DROP COLUMN company_id;
ALTER TABLE job_post RENAME COLUMN new_company_id TO company_id;
ALTER TABLE cover_letter DROP COLUMN company_id;
ALTER TABLE cover_letter RENAME COLUMN new_company_id TO company_id;

-- 7. Recreate the self-FK (NanoID -> NanoID) + its btree index.
ALTER TABLE company ADD CONSTRAINT company_canonical_id_fk
    FOREIGN KEY (canonical_id) REFERENCES company (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX company_canonical_id_idx ON company (canonical_id);

-- 8. Recreate every dependent FK (DEFERRABLE INITIALLY DEFERRED, matching
--    Django; on_delete is emulated in the ORM, never in the DB FK) + its
--    btree index (the company_alias / federation_followers index doubles as
--    the Meta Index(company)).
ALTER TABLE federation_actors ADD CONSTRAINT federation_actors_company_id_fk
    FOREIGN KEY (company_id) REFERENCES company (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX federation_actors_company_id_idx ON federation_actors (company_id);
ALTER TABLE experience ADD CONSTRAINT experience_company_id_fk
    FOREIGN KEY (company_id) REFERENCES company (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX experience_company_id_idx ON experience (company_id);
ALTER TABLE question ADD CONSTRAINT question_company_id_fk
    FOREIGN KEY (company_id) REFERENCES company (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX question_company_id_idx ON question (company_id);
ALTER TABLE job_application ADD CONSTRAINT job_application_company_id_fk
    FOREIGN KEY (company_id) REFERENCES company (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX job_application_company_id_idx ON job_application (company_id);
ALTER TABLE federation_followers ADD CONSTRAINT federation_followers_company_id_fk
    FOREIGN KEY (company_id) REFERENCES company (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX federation_followers_company_id_idx ON federation_followers (company_id);
ALTER TABLE scrape ADD CONSTRAINT scrape_company_id_fk
    FOREIGN KEY (company_id) REFERENCES company (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX scrape_company_id_idx ON scrape (company_id);
ALTER TABLE company_alias ADD CONSTRAINT company_alias_company_id_fk
    FOREIGN KEY (company_id) REFERENCES company (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX company_alias_company_id_idx ON company_alias (company_id);
ALTER TABLE job_post ADD CONSTRAINT job_post_company_id_fk
    FOREIGN KEY (company_id) REFERENCES company (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX job_post_company_id_idx ON job_post (company_id);
ALTER TABLE cover_letter ADD CONSTRAINT cover_letter_company_id_fk
    FOREIGN KEY (company_id) REFERENCES company (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX cover_letter_company_id_idx ON cover_letter (company_id);

-- 9. Recreate the CHECK constraints + partial UNIQUE on the NanoID values.
ALTER TABLE company ADD CONSTRAINT company_canonical_not_self
    CHECK (canonical_id IS NULL OR canonical_id <> id);
ALTER TABLE federation_actors ADD CONSTRAINT actor_user_company_mutually_exclusive
    CHECK (user_id IS NULL OR company_id IS NULL);
ALTER TABLE federation_followers ADD CONSTRAINT federation_follower_followee_required
    CHECK (local_user_id IS NOT NULL OR company_id IS NOT NULL);
CREATE UNIQUE INDEX federation_follower_unique_company_remote
    ON federation_followers (company_id, actor_uri) WHERE company_id IS NOT NULL;
"""

# --- Reverse: NanoID PK -> a FRESH integer PK -----------------------------

SWAP_REVERSE = """
-- 0. IMMEDIATE constraint mode for this transaction (self-FK, as above).
SET CONSTRAINTS ALL IMMEDIATE;

-- 1. Integer staging columns on the parent + every FK-bearing table.
ALTER TABLE company ADD COLUMN old_int_id bigint;
ALTER TABLE company ADD COLUMN old_int_canonical_id bigint;
ALTER TABLE federation_actors ADD COLUMN old_int_company_id bigint;
ALTER TABLE experience ADD COLUMN old_int_company_id bigint;
ALTER TABLE question ADD COLUMN old_int_company_id bigint;
ALTER TABLE job_application ADD COLUMN old_int_company_id bigint;
ALTER TABLE federation_followers ADD COLUMN old_int_company_id bigint;
ALTER TABLE scrape ADD COLUMN old_int_company_id bigint;
ALTER TABLE company_alias ADD COLUMN old_int_company_id bigint;
ALTER TABLE job_post ADD COLUMN old_int_company_id bigint;
ALTER TABLE cover_letter ADD COLUMN old_int_company_id bigint;

-- 2. Assign fresh sequential ints (1..N) deterministically by current id.
WITH numbered AS (
    SELECT id, row_number() OVER (ORDER BY id) AS rn FROM company
)
UPDATE company c SET old_int_id = n.rn FROM numbered n WHERE c.id = n.id;

-- 3. Repoint self-FK + dependent staging columns by joining on the NanoID.
UPDATE company c SET old_int_canonical_id = p.old_int_id
  FROM company p WHERE c.canonical_id = p.id;
UPDATE federation_actors t SET old_int_company_id = co.old_int_id
  FROM company co WHERE t.company_id = co.id;
UPDATE experience t SET old_int_company_id = co.old_int_id
  FROM company co WHERE t.company_id = co.id;
UPDATE question t SET old_int_company_id = co.old_int_id
  FROM company co WHERE t.company_id = co.id;
UPDATE job_application t SET old_int_company_id = co.old_int_id
  FROM company co WHERE t.company_id = co.id;
UPDATE federation_followers t SET old_int_company_id = co.old_int_id
  FROM company co WHERE t.company_id = co.id;
UPDATE scrape t SET old_int_company_id = co.old_int_id
  FROM company co WHERE t.company_id = co.id;
UPDATE company_alias t SET old_int_company_id = co.old_int_id
  FROM company co WHERE t.company_id = co.id;
UPDATE job_post t SET old_int_company_id = co.old_int_id
  FROM company co WHERE t.company_id = co.id;
UPDATE cover_letter t SET old_int_company_id = co.old_int_id
  FROM company co WHERE t.company_id = co.id;

-- 4. Drop all FKs referencing company (dependent + self), catalog-resolved.
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT conrelid::regclass AS tbl, conname
          FROM pg_constraint
         WHERE confrelid = 'company'::regclass AND contype = 'f'
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.tbl, r.conname);
    END LOOP;
END $$;
DROP INDEX IF EXISTS company_canonical_id_idx;
DROP INDEX IF EXISTS federation_actors_company_id_idx;
DROP INDEX IF EXISTS experience_company_id_idx;
DROP INDEX IF EXISTS question_company_id_idx;
DROP INDEX IF EXISTS job_application_company_id_idx;
DROP INDEX IF EXISTS federation_followers_company_id_idx;
DROP INDEX IF EXISTS scrape_company_id_idx;
DROP INDEX IF EXISTS company_alias_company_id_idx;
DROP INDEX IF EXISTS job_post_company_id_idx;
DROP INDEX IF EXISTS cover_letter_company_id_idx;

-- 5. Drop the named CHECK / partial-UNIQUE objects (rebuilt on ints).
ALTER TABLE company DROP CONSTRAINT IF EXISTS company_canonical_not_self;
ALTER TABLE federation_actors DROP CONSTRAINT IF EXISTS actor_user_company_mutually_exclusive;
ALTER TABLE federation_followers DROP CONSTRAINT IF EXISTS federation_follower_followee_required;
DROP INDEX IF EXISTS federation_follower_unique_company_remote;

-- 6. Drop the NanoID PK and promote the int column to a real identity PK.
DO $$
DECLARE cname text;
BEGIN
    SELECT conname INTO cname FROM pg_constraint
     WHERE conrelid = 'company'::regclass AND contype = 'p';
    EXECUTE format('ALTER TABLE company DROP CONSTRAINT %I', cname);
END $$;
ALTER TABLE company DROP COLUMN id;
ALTER TABLE company RENAME COLUMN old_int_id TO id;
ALTER TABLE company ALTER COLUMN id SET NOT NULL;
ALTER TABLE company ADD CONSTRAINT company_pkey PRIMARY KEY (id);
ALTER TABLE company ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY;
SELECT setval(pg_get_serial_sequence('company', 'id'),
              (SELECT COALESCE(max(id), 1) FROM company));

-- 7. Self-FK column back to int.
ALTER TABLE company DROP COLUMN canonical_id;
ALTER TABLE company RENAME COLUMN old_int_canonical_id TO canonical_id;

-- 8. Dependent FK columns back to int (+ NOT NULL on company_alias).
ALTER TABLE federation_actors DROP COLUMN company_id;
ALTER TABLE federation_actors RENAME COLUMN old_int_company_id TO company_id;
ALTER TABLE experience DROP COLUMN company_id;
ALTER TABLE experience RENAME COLUMN old_int_company_id TO company_id;
ALTER TABLE question DROP COLUMN company_id;
ALTER TABLE question RENAME COLUMN old_int_company_id TO company_id;
ALTER TABLE job_application DROP COLUMN company_id;
ALTER TABLE job_application RENAME COLUMN old_int_company_id TO company_id;
ALTER TABLE federation_followers DROP COLUMN company_id;
ALTER TABLE federation_followers RENAME COLUMN old_int_company_id TO company_id;
ALTER TABLE scrape DROP COLUMN company_id;
ALTER TABLE scrape RENAME COLUMN old_int_company_id TO company_id;
ALTER TABLE company_alias DROP COLUMN company_id;
ALTER TABLE company_alias RENAME COLUMN old_int_company_id TO company_id;
ALTER TABLE company_alias ALTER COLUMN company_id SET NOT NULL;
ALTER TABLE job_post DROP COLUMN company_id;
ALTER TABLE job_post RENAME COLUMN old_int_company_id TO company_id;
ALTER TABLE cover_letter DROP COLUMN company_id;
ALTER TABLE cover_letter RENAME COLUMN old_int_company_id TO company_id;

-- 9. Recreate the self-FK + index.
ALTER TABLE company ADD CONSTRAINT company_canonical_id_fk
    FOREIGN KEY (canonical_id) REFERENCES company (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX company_canonical_id_idx ON company (canonical_id);

-- 10. Recreate the dependent FKs + indexes.
ALTER TABLE federation_actors ADD CONSTRAINT federation_actors_company_id_fk
    FOREIGN KEY (company_id) REFERENCES company (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX federation_actors_company_id_idx ON federation_actors (company_id);
ALTER TABLE experience ADD CONSTRAINT experience_company_id_fk
    FOREIGN KEY (company_id) REFERENCES company (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX experience_company_id_idx ON experience (company_id);
ALTER TABLE question ADD CONSTRAINT question_company_id_fk
    FOREIGN KEY (company_id) REFERENCES company (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX question_company_id_idx ON question (company_id);
ALTER TABLE job_application ADD CONSTRAINT job_application_company_id_fk
    FOREIGN KEY (company_id) REFERENCES company (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX job_application_company_id_idx ON job_application (company_id);
ALTER TABLE federation_followers ADD CONSTRAINT federation_followers_company_id_fk
    FOREIGN KEY (company_id) REFERENCES company (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX federation_followers_company_id_idx ON federation_followers (company_id);
ALTER TABLE scrape ADD CONSTRAINT scrape_company_id_fk
    FOREIGN KEY (company_id) REFERENCES company (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX scrape_company_id_idx ON scrape (company_id);
ALTER TABLE company_alias ADD CONSTRAINT company_alias_company_id_fk
    FOREIGN KEY (company_id) REFERENCES company (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX company_alias_company_id_idx ON company_alias (company_id);
ALTER TABLE job_post ADD CONSTRAINT job_post_company_id_fk
    FOREIGN KEY (company_id) REFERENCES company (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX job_post_company_id_idx ON job_post (company_id);
ALTER TABLE cover_letter ADD CONSTRAINT cover_letter_company_id_fk
    FOREIGN KEY (company_id) REFERENCES company (id) DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX cover_letter_company_id_idx ON cover_letter (company_id);

-- 11. Recreate the CHECK constraints + partial UNIQUE on the int values.
ALTER TABLE company ADD CONSTRAINT company_canonical_not_self
    CHECK (canonical_id IS NULL OR canonical_id <> id);
ALTER TABLE federation_actors ADD CONSTRAINT actor_user_company_mutually_exclusive
    CHECK (user_id IS NULL OR company_id IS NULL);
ALTER TABLE federation_followers ADD CONSTRAINT federation_follower_followee_required
    CHECK (local_user_id IS NOT NULL OR company_id IS NOT NULL);
CREATE UNIQUE INDEX federation_follower_unique_company_remote
    ON federation_followers (company_id, actor_uri) WHERE company_id IS NOT NULL;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0123_resume_nanoid_pk_swap"),
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
                    model_name="company",
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
