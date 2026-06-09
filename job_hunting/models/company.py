from django.db import models
from .base import GetMixin


class Company(GetMixin, models.Model):
    name = models.CharField(max_length=255, unique=True)
    display_name = models.CharField(max_length=255, null=True, blank=True)
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "company"

    def __str__(self):
        return self.display_name or self.name

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
