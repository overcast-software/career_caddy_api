from django.db import models, transaction
from django.db.models import F, Q
from .base import GetMixin


class Company(GetMixin, models.Model):
    SOURCE_EXTRACTION = "extraction"
    SOURCE_MANUAL = "manual"
    SOURCE_BACKFILL = "backfill"

    SOURCES = [
        (SOURCE_EXTRACTION, "extraction"),
        (SOURCE_MANUAL, "manual"),
        (SOURCE_BACKFILL, "backfill"),
    ]

    name = models.CharField(max_length=255, unique=True)
    display_name = models.CharField(max_length=255, null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
    # Phase A: provenance carried over from CompanyAlias.source on
    # the migrated rows. Nullable because existing pre-Phase-A
    # Companies have no recorded source.
    source = models.CharField(
        max_length=32, choices=SOURCES, null=True, blank=True
    )
    # Phase A: slug carried over from CompanyAlias.name_slug so the
    # alias gate can lookup against Company directly in Phase B.
    # Not declared unique at the column level because the legacy
    # backfill (0098) collision-skipped rows whose slugs collided —
    # mirroring that data here would 500 the migration. Phase B
    # cleans up + tightens uniqueness.
    name_slug = models.CharField(
        max_length=255, null=True, blank=True, db_index=True
    )
    created_at = models.DateTimeField(null=True, blank=True)
    # Phase A self-FK: an "alias" Company points its `canonical` at the
    # true Company. NULL means this row IS canonical. SET_NULL on
    # delete so deleting the canonical strands the alias rather than
    # cascading-deleting it (data preservation; staff can re-alias).
    canonical = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="aliases",
        db_index=True,
    )

    class Meta:
        db_table = "company"
        constraints = [
            # Belt-and-suspenders DB check: `mark_as_alias_of` already
            # rejects self-target in Python, but the constraint blocks
            # any future write path (admin, shell, migration) from
            # corrupting the graph with a self-loop.
            models.CheckConstraint(
                condition=Q(canonical__isnull=True) | ~Q(canonical=F("id")),
                name="company_canonical_not_self",
            ),
        ]

    def __str__(self):
        return self.display_name or self.name

    def mark_as_alias_of(self, target_id, source="manual"):
        """Set ``self.canonical_id = target_id``, flattening alias chains.

        Service verb for the Phase A self-FK alias model. Atomic.
        Side effects:

        - Sets ``self.canonical_id = target_id``.
        - Re-points every Company whose ``canonical_id = self.id``
          (the rows that previously aliased self) at ``target_id``,
          so the graph stays one-level deep: an alias points only at
          a true canonical, never at another alias.
        - If ``target`` itself has a non-null ``canonical_id``, walks
          the chain to its true root and aliases self there instead.
          Prevents two-hop chains from forming when the caller passes
          an intermediate alias as the target.

        Raises:
        - ``ValueError`` on self-target (``target_id == self.id``).
        - ``ValueError`` on cycle attempt (target's canonical chain
          loops back through self).
        - ``Company.DoesNotExist`` when ``target_id`` is not a real id.

        Idempotent: calling twice with the same already-applied
        target_id is a no-op on the second call.

        Returns the updated ``self`` (refreshed from DB).
        """
        if target_id == self.id:
            raise ValueError(
                f"Cannot alias Company id={self.id} to itself."
            )

        with transaction.atomic():
            target = Company.objects.select_for_update().get(pk=target_id)

            # Walk target's canonical chain to its true root. Detects
            # cycles by tracking visited ids. The chain SHOULD be at
            # most one hop (we maintain that invariant), but a bad
            # actor / admin write could have left a longer chain;
            # follow it defensively.
            visited = {self.id}
            root = target
            while root.canonical_id is not None:
                if root.canonical_id in visited:
                    raise ValueError(
                        f"Cycle: Company id={self.id} cannot alias to "
                        f"id={target_id} — the chain loops back through self."
                    )
                visited.add(root.id)
                root = Company.objects.select_for_update().get(
                    pk=root.canonical_id
                )

            if root.id == self.id:
                raise ValueError(
                    f"Cycle: Company id={self.id} cannot alias to "
                    f"id={target_id} — the chain loops back through self."
                )

            # Idempotent fast-path: already aliased at root.
            if self.canonical_id == root.id:
                return self

            # Re-point any Company currently aliased AT self → root.
            # Without this, the graph would briefly have a two-hop
            # chain (alias → self → root). The invariant: every alias
            # points at a true canonical (canonical_id IS NULL on the
            # target), never at another alias.
            Company.objects.filter(canonical_id=self.id).update(
                canonical_id=root.id
            )

            self.canonical_id = root.id
            self.save(update_fields=["canonical"])

        self.refresh_from_db()
        return self

    @classmethod
    def find_by_alias(cls, name):
        """Return the Company whose slugged name matches ``name``, or None.

        Phase A of the dedupe redesign. Computes
        ``slug(strip_corp_suffix(name))`` and looks the result up
        against ``CompanyAlias.name_slug`` (globally unique). The match
        is *exact* on the slug — Levenshtein / trigram-fuzzy hits are
        deliberately NOT auto-attached at this gate; they surface as
        suggestions on the Scrape for staff review (see
        ``JobPostExtractor.process_evaluation`` and
        ``Scrape.company_suggestions``).

        Returns ``None`` when:
        - ``name`` is empty / whitespace-only;
        - the computed slug is empty (e.g. all-punctuation input);
        - no ``CompanyAlias`` row exists for the slug.

        Defensive against ``CompanyAlias`` rows whose ``company`` FK
        has been cascaded away (shouldn't happen given the model's
        ``on_delete=CASCADE``, but the join is null-safe regardless).
        """
        from job_hunting.lib.slug import slug, strip_corp_suffix
        from .company_alias import CompanyAlias

        if not name:
            return None
        candidate_slug = slug(strip_corp_suffix(name))
        if not candidate_slug:
            return None
        alias = (
            CompanyAlias.objects.select_related("company")
            .filter(name_slug=candidate_slug)
            .first()
        )
        if alias is None:
            return None
        return alias.company
