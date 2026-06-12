"""Phase A data migration — mirror CompanyAlias rows into Company self-FK.

For every existing ``CompanyAlias`` row:

- ``target_slug = alias.name_slug``.
- If a ``Company`` already exists with that ``name_slug``, set its
  ``canonical_id = alias.company_id`` (only when currently NULL —
  don't overwrite a real pointer) and adopt ``source = alias.source``
  only when currently NULL — don't overwrite a real value.
- Else create a fresh ``Company``:
  ``name = alias.name``,
  ``name_slug = alias.name_slug``,
  ``source = alias.source``,
  ``canonical_id = alias.company_id``,
  ``created_at = alias.created_at`` (preserves original
  alias-creation timestamp instead of "now").

Defensive cases (logged at WARNING, skipped):

- ``alias.company_id`` no longer exists in the company table — orphan.
- ``alias.name_slug`` matches a Company whose id IS ``alias.company_id``
  — that's the backfill self-alias from 0098; nothing to do.
- ``alias.name`` collides with an existing ``Company.name`` (UNIQUE)
  but its slug doesn't match — surfacing this means a human-written
  name collides with an alias name variant. Log + skip (lets staff
  triage via merge-into).

The ``CompanyAlias`` table is NOT dropped here. Phase C drops it
after the frontend reconciles to read aliases via
``GET /api/v1/companies/:id/aliases/``.
"""

import logging

from django.db import migrations


def forwards(apps, schema_editor):
    logger = logging.getLogger(__name__)
    Company = apps.get_model("job_hunting", "Company")
    CompanyAlias = apps.get_model("job_hunting", "CompanyAlias")

    company_ids = set(Company.objects.values_list("id", flat=True))
    # name → id and slug → id snapshots BEFORE we start creating
    # rows, so each iteration sees a deterministic baseline. We
    # incrementally add new rows to both maps as we mint them.
    name_to_id = dict(Company.objects.values_list("name", "id"))
    slug_to_id = dict(
        Company.objects.filter(name_slug__isnull=False).values_list(
            "name_slug", "id"
        )
    )

    created = 0
    pointed = 0
    backfilled_slug = 0
    skipped_orphan = 0
    skipped_self_alias = 0
    skipped_name_collision = 0
    skipped_empty = 0

    for alias in CompanyAlias.objects.all().iterator():
        if not alias.name or not alias.name_slug:
            logger.warning(
                "0104 mirror: CompanyAlias id=%s has empty name/slug; skipping.",
                alias.id,
            )
            skipped_empty += 1
            continue
        if alias.company_id not in company_ids:
            logger.warning(
                "0104 mirror: CompanyAlias id=%s points at deleted "
                "Company id=%s; skipping.",
                alias.id, alias.company_id,
            )
            skipped_orphan += 1
            continue

        slug_owner_id = slug_to_id.get(alias.name_slug)

        # Name-based lookup catches two cases:
        # 1. The backfill self-alias from 0098 (alias.name ==
        #    Company.name where Company.id == alias.company_id).
        # 2. A name-only collision where another Company shares
        #    alias.name (rare — would have collided on
        #    Company.name UNIQUE already).
        name_owner_id = name_to_id.get(alias.name)

        # Backfill self-alias: alias.name matches the canonical
        # Company's own name. Use this row to backfill name_slug +
        # source on the existing canonical, then continue.
        if name_owner_id == alias.company_id:
            existing = Company.objects.get(pk=name_owner_id)
            changed = []
            if existing.name_slug is None:
                existing.name_slug = alias.name_slug
                slug_to_id[alias.name_slug] = existing.id
                changed.append("name_slug")
            if existing.source is None and alias.source:
                existing.source = alias.source
                changed.append("source")
            if existing.created_at is None and alias.created_at:
                existing.created_at = alias.created_at
                changed.append("created_at")
            if changed:
                existing.save(update_fields=changed)
                backfilled_slug += 1
            skipped_self_alias += 1
            continue

        if slug_owner_id is not None:
            if slug_owner_id == alias.company_id:
                # Slug-based self-alias (same row as name-based
                # path above, hit via the slug lookup). Nothing to do.
                skipped_self_alias += 1
                continue
            # Another Company already carries this slug — adopt the
            # canonical pointer (when none set) and the source (when
            # none set). Never overwrite a real value.
            existing = Company.objects.get(pk=slug_owner_id)
            changed = []
            if existing.canonical_id is None:
                existing.canonical_id = alias.company_id
                changed.append("canonical")
            elif existing.canonical_id != alias.company_id:
                logger.warning(
                    "0104 mirror: Company id=%s already canonical=%s; "
                    "alias id=%s wanted to set canonical=%s — leaving "
                    "the existing pointer alone.",
                    slug_owner_id, existing.canonical_id,
                    alias.id, alias.company_id,
                )
            if existing.source is None and alias.source:
                existing.source = alias.source
                changed.append("source")
            if changed:
                existing.save(update_fields=changed)
                pointed += 1
            continue

        # New Company row for this name variant. Guard against
        # alias.name colliding with a DIFFERENT existing Company.name
        # on the UNIQUE constraint (slug differs, names match — rare).
        if name_owner_id is not None and name_owner_id != alias.company_id:
            logger.warning(
                "0104 mirror: CompanyAlias id=%s name=%r slug=%r — name "
                "collides with existing Company id=%s on UNIQUE(name); "
                "skipping. Staff resolution via merge-into.",
                alias.id, alias.name, alias.name_slug, name_owner_id,
            )
            skipped_name_collision += 1
            continue

        new_company = Company.objects.create(
            name=alias.name,
            name_slug=alias.name_slug,
            source=alias.source,
            canonical_id=alias.company_id,
            created_at=alias.created_at,
        )
        name_to_id[new_company.name] = new_company.id
        slug_to_id[new_company.name_slug] = new_company.id
        company_ids.add(new_company.id)
        created += 1

    logger.info(
        "0104 mirror summary: created=%s pointed=%s backfilled_slug=%s "
        "skipped_self_alias=%s skipped_orphan=%s "
        "skipped_name_collision=%s skipped_empty=%s",
        created, pointed, backfilled_slug, skipped_self_alias,
        skipped_orphan, skipped_name_collision, skipped_empty,
    )


def reverse(apps, schema_editor):
    """Reverse: NULL out every canonical pointer.

    New Company rows minted forward are NOT deleted on reverse —
    they'd lose their distinguishing source field after 0103 also
    rolls back. Leaving them as standalone Companies with
    canonical=NULL keeps any FKs from JobPost/Scrape (none should
    exist yet — Phase B repoints those) intact.
    """
    Company = apps.get_model("job_hunting", "Company")
    Company.objects.filter(canonical__isnull=False).update(canonical=None)


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0103_company_canonical_self_fk"),
    ]

    operations = [
        migrations.RunPython(forwards, reverse),
    ]
