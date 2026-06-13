"""Backfill ``Company.slug`` (idempotent).

Phase 6a of Plans/PLAN Fediverse Phase 6 — populates the new
``Company.slug`` column (the WebFinger handle, not the dedupe
``name_slug``) for every Company that doesn't already have one. Run
once after applying migration 0106; safe to re-run.

Rules:
- Skip rows whose ``slug`` is already set — staff or a previous run
  picked the canonical value, don't clobber.
- Compute candidate via Django's ``slugify(name)`` truncated to 80
  chars. The dedupe-side ``slug(strip_corp_suffix(name))`` is the
  WRONG normalisation here — two Companies with the same dedupe slug
  must still surface as distinct federation handles when they live
  as separate Company rows (e.g. an unmerged duplicate pair).
- Collision suffix: when the candidate is taken, append ``-<id>``.
  Using the row's own id keeps the choice deterministic and replay-
  safe — re-running the command on the same dataset converges.
- Empty candidate (all-punctuation name): fall back to
  ``company-<id>`` so every row is reachable via WebFinger.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils.text import slugify

from job_hunting.models import Company


SLUG_MAX_LENGTH = 80


class Command(BaseCommand):
    help = "Populate Company.slug for rows that don't already have one (idempotent)."

    def handle(self, *args, **options):
        existing_slugs: set[str] = set(
            Company.objects.exclude(slug__isnull=True)
            .exclude(slug="")
            .values_list("slug", flat=True)
        )

        updated = 0
        skipped = 0

        # Iterate ordered by id so collision-suffix output is stable
        # across runs — a newcomer with the same name as an existing
        # row gets the ``-<id>`` form, the older row keeps the bare slug.
        for company in Company.objects.filter(slug__isnull=True).order_by("id").iterator():
            candidate = self._candidate_slug(company.name, company.id)
            if candidate in existing_slugs:
                candidate = self._collision_suffix(candidate, company.id)
                # Truly-pathological case — the suffix variant also
                # collides. Walk up an integer counter until we find a
                # free slot. Realistically untriggered; defensive only.
                counter = 2
                while candidate in existing_slugs:
                    candidate = self._collision_suffix(
                        self._candidate_slug(company.name, company.id),
                        company.id,
                        extra=counter,
                    )
                    counter += 1
            Company.objects.filter(pk=company.id).update(slug=candidate)
            existing_slugs.add(candidate)
            updated += 1

        skipped = Company.objects.exclude(slug__isnull=True).exclude(slug="").count() - updated

        self.stdout.write(
            self.style.SUCCESS(
                f"company slugs — updated={updated} already_set={skipped}"
            )
        )

    @staticmethod
    def _candidate_slug(name: str, row_id: int) -> str:
        base = slugify(name or "")[:SLUG_MAX_LENGTH].strip("-")
        if not base:
            return f"company-{row_id}"
        return base

    @staticmethod
    def _collision_suffix(base: str, row_id: int, *, extra: int = 1) -> str:
        suffix = f"-{row_id}" if extra == 1 else f"-{row_id}-{extra}"
        # Truncate base so the combined length still fits SLUG_MAX_LENGTH.
        room = SLUG_MAX_LENGTH - len(suffix)
        if room < 1:
            # Pathological: id alone is longer than the column. Use
            # the suffix unmodified; the column will still accept it
            # because suffix length stays well under 80 in practice.
            return suffix.lstrip("-")
        return f"{base[:room].rstrip('-')}{suffix}"
