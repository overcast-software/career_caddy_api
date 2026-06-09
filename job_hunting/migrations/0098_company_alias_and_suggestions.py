"""Phase A of the dedupe redesign — Company alias gate.

Three schema changes + one data backfill + one Postgres extension:

1. ``CompanyAlias`` table — one row per name variant for a Company.
   ``name_slug`` is globally unique. Indexed on ``name_slug`` and
   ``company`` for the two read paths (``Company.find_by_alias`` lookup,
   ``company.aliases`` reverse-related queries).
2. ``Scrape.company_suggestions JSONField`` — top-3 trigram-similar
   companies stashed at extraction time when no exact alias hit fired.
   Read by the frontend "Suggested companies" callout on Scrape show.
3. ``DuplicateAnnotation.action`` choice extension — adds
   ``company_merge`` so the merge-into endpoint can write audit rows.
   Django records choice changes as an ``AlterField`` (no DB change).
4. ``pg_trgm`` Postgres extension — required by the extractor's
   trigram-similarity suggestion query. ``CREATE EXTENSION IF NOT
   EXISTS pg_trgm`` is idempotent and safe to re-run.
5. Data backfill — one ``CompanyAlias`` row per existing
   ``Company.name`` (``source="backfill"``). Two Companies whose
   names slug to the same key are a duplicate that staff must
   resolve via merge-into; we log them at WARNING and keep the
   first row only.

``atomic = False`` — the ``CREATE EXTENSION`` call must commit
before the RunPython backfill issues trigram-aware queries (none
of the backfill below uses trigrams, but the CREATE EXTENSION
issued mid-transaction has bitten this project in adjacent
migrations and the conservative split keeps each operation
independent).
"""

from django.db import migrations, models


def backfill_company_aliases(apps, schema_editor):
    """Write one alias row per existing Company.

    For every existing Company, compute the normalized slug from its
    ``name`` and create a ``CompanyAlias`` row with ``source='backfill'``.

    Slug collisions: when two Companies normalize to the same slug,
    only the first wins. The second is logged at WARNING level so a
    staff curator can resolve the duplicate via the new ``POST
    /api/v1/companies/:id/merge-into/`` endpoint.

    Uses the historical model + a local copy of the slug helper —
    cannot import from ``job_hunting.lib.slug`` at app boot time
    because the migration framework loads migrations before the
    app is fully wired. Inline copy is short and stable.
    """
    import logging
    import re
    import unicodedata

    logger = logging.getLogger(__name__)
    Company = apps.get_model("job_hunting", "Company")
    CompanyAlias = apps.get_model("job_hunting", "CompanyAlias")

    _DASH_TRANSLATION = {
        0x2010: "-", 0x2011: "-", 0x2012: "-", 0x2013: "-",
        0x2014: "-", 0x2015: "-", 0x2212: "-",
    }
    _QUOTE_TRANSLATION = {
        0x2018: "'", 0x2019: "'", 0x201A: "'", 0x201B: "'",
        0x201C: '"', 0x201D: '"', 0x201E: '"', 0x201F: '"',
    }
    _TRANSLATION_TABLE = {**_DASH_TRANSLATION, **_QUOTE_TRANSLATION}
    _NON_SLUG = re.compile(r"[^a-z0-9\- ]+")
    _WS = re.compile(r"\s+")
    _HYPHEN_OR_SPACE = re.compile(r"[\s-]+")

    _CORP_SUFFIXES = [
        "insurance company", "holdings group", "holding company",
        "corporation", "incorporated", "limited liability company",
        "limited partnership", "limited", "company", "holdings",
        "holding", "group", "corp", "co", "inc", "llc", "ltd",
        "lp", "llp", "plc", "ag", "gmbh", "sa", "nv", "bv",
    ]
    _SUFFIX_PAT = "|".join(re.escape(s) for s in _CORP_SUFFIXES)
    _SUFFIX_RE = re.compile(
        rf"(?:[\s,.\-]+\b(?:{_SUFFIX_PAT})\b\.?)+\Z",
        flags=re.IGNORECASE,
    )

    def _slug(s):
        if not s:
            return ""
        n = unicodedata.normalize("NFKC", s).translate(_TRANSLATION_TABLE)
        n = _WS.sub(" ", n.lower()).strip()
        n = _NON_SLUG.sub("", n)
        n = _HYPHEN_OR_SPACE.sub("-", n).strip("-")
        return n

    def _strip_corp_suffix(name):
        if not name:
            return ""
        stripped = name.strip()
        while True:
            new = _SUFFIX_RE.sub("", stripped).rstrip(" ,.–—-")
            if new == stripped or not new:
                return new or stripped
            stripped = new

    seen_slugs = {}
    for company in Company.objects.all().iterator():
        candidate_slug = _slug(_strip_corp_suffix(company.name or ""))
        if not candidate_slug:
            logger.warning(
                "Backfill: Company id=%s name=%r produced an empty slug; skipping.",
                company.id, company.name,
            )
            continue
        if candidate_slug in seen_slugs:
            logger.warning(
                "Backfill: Company id=%s name=%r collides on slug=%r "
                "with Company id=%s; skipping. Staff must resolve via "
                "POST /api/v1/companies/<id>/merge-into/.",
                company.id, company.name, candidate_slug,
                seen_slugs[candidate_slug],
            )
            continue
        CompanyAlias.objects.create(
            company_id=company.id,
            name=company.name,
            name_slug=candidate_slug,
            source="backfill",
        )
        seen_slugs[candidate_slug] = company.id


def reverse_backfill(apps, schema_editor):
    """Drop every backfill-sourced alias row. The schema removal of
    CompanyAlias by the reverse pass handles the table itself."""
    CompanyAlias = apps.get_model("job_hunting", "CompanyAlias")
    CompanyAlias.objects.filter(source="backfill").delete()


class Migration(migrations.Migration):

    # See module docstring for the atomicity discussion. CREATE
    # EXTENSION should commit before subsequent operations that may
    # depend on it; splitting transaction boundaries via atomic=False
    # makes that explicit.
    atomic = False

    dependencies = [
        ("job_hunting", "0097_jobpost_last_seen_at"),
    ]

    operations = [
        migrations.RunSQL(
            "CREATE EXTENSION IF NOT EXISTS pg_trgm",
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.CreateModel(
            name="CompanyAlias",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=255)),
                (
                    "name_slug",
                    models.CharField(db_index=True, max_length=255, unique=True),
                ),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("extraction", "extraction"),
                            ("manual", "manual"),
                            ("backfill", "backfill"),
                        ],
                        max_length=32,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "company",
                    models.ForeignKey(
                        on_delete=models.deletion.CASCADE,
                        related_name="aliases",
                        to="job_hunting.company",
                    ),
                ),
            ],
            options={
                "db_table": "company_alias",
            },
        ),
        migrations.AddIndex(
            model_name="companyalias",
            index=models.Index(
                fields=["company"], name="company_ali_company_idx"
            ),
        ),
        migrations.AddField(
            model_name="scrape",
            name="company_suggestions",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="duplicateannotation",
            name="action",
            field=models.CharField(
                choices=[
                    ("mark", "mark"),
                    ("unlink", "unlink"),
                    ("promote", "promote"),
                    ("historical", "historical"),
                    ("federated_merge", "federated_merge"),
                    ("company_merge", "company_merge"),
                ],
                max_length=16,
            ),
        ),
        migrations.RunPython(backfill_company_aliases, reverse_backfill),
    ]
