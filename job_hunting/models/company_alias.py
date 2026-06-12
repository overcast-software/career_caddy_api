"""Company name-variant alias rows.

One row per name variant for a Company. The unique key is
``name_slug`` (normalized via ``job_hunting.lib.slug``); the
human-readable ``name`` is stored alongside for staff review and
display. Backed by the migration that bootstraps one row per
existing Company.name, with future writes from:

- the extractor: when a fresh Company is minted, an alias row is
  written for the literal extracted name (source="extraction").
- staff: manual additions via admin / merge-into.
- federation / future: anything that learns a new variant for an
  existing Company.

The unique constraint on ``name_slug`` is global (not scoped to
Company) because two Companies that slug to the same key
indicate a duplicate that needs human resolution via the
merge-into endpoint — the system surfaces the collision rather
than silently choosing one side.

See parent plan ``go-over-this-plan-staged-sutherland.md`` Phase A
and api notes.org ``Architecture/Dedupe pipeline contract``.
"""

from django.db import models


class CompanyAlias(models.Model):
    SOURCE_EXTRACTION = "extraction"
    SOURCE_MANUAL = "manual"
    SOURCE_BACKFILL = "backfill"

    SOURCES = [
        (SOURCE_EXTRACTION, "extraction"),
        (SOURCE_MANUAL, "manual"),
        (SOURCE_BACKFILL, "backfill"),
    ]

    # Phase A self-FK on Company claimed the `aliases` reverse
    # accessor (canonical→aliases on Company itself). This legacy
    # reverse renames to `legacy_aliases` so the two don't collide.
    # The model + table goes away in Phase C; no live caller uses
    # this reverse accessor today (only the alias.company forward
    # FK is read), so renaming is safe.
    company = models.ForeignKey(
        "Company",
        on_delete=models.CASCADE,
        related_name="legacy_aliases",
    )
    name = models.CharField(max_length=255)
    name_slug = models.CharField(max_length=255, db_index=True, unique=True)
    source = models.CharField(max_length=32, choices=SOURCES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "company_alias"
        indexes = [
            models.Index(fields=["company"]),
        ]

    def __str__(self):
        return f"{self.name} → {self.company.name}"
