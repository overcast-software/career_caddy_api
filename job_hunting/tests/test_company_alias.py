"""Tests for the CompanyAlias model + Company.find_by_alias gate
+ JobPostExtractor company-resolution behavior + the merge-into
endpoint. Phase A of the dedupe redesign.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from job_hunting.lib.parsers.job_post_extractor import (
    JobPostExtractor,
    ParsedJobData,
)
from job_hunting.lib.slug import slug, strip_corp_suffix
from job_hunting.models import (
    Company,
    CompanyAlias,
    DuplicateAnnotation,
    JobApplication,
    JobPost,
    Scrape,
)

User = get_user_model()


class TestCompanyAliasModel(TestCase):
    def test_create(self):
        company = Company.objects.create(name="Acme Corp")
        alias = CompanyAlias.objects.create(
            company=company,
            name="Acme Corp",
            name_slug=slug(strip_corp_suffix("Acme Corp")),
            source=CompanyAlias.SOURCE_BACKFILL,
        )
        self.assertEqual(alias.company_id, company.id)
        self.assertEqual(str(alias), "Acme Corp → Acme Corp")

    def test_name_slug_is_unique(self):
        from django.db import IntegrityError, transaction
        c1 = Company.objects.create(name="Acme Corporation")
        c2 = Company.objects.create(name="Acme Inc.")
        CompanyAlias.objects.create(
            company=c1, name="Acme Corporation",
            name_slug="acme", source=CompanyAlias.SOURCE_BACKFILL,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CompanyAlias.objects.create(
                    company=c2, name="Acme Inc.",
                    name_slug="acme", source=CompanyAlias.SOURCE_BACKFILL,
                )

    def test_cascade_on_company_delete(self):
        company = Company.objects.create(name="Acme")
        CompanyAlias.objects.create(
            company=company, name="Acme",
            name_slug="acme", source=CompanyAlias.SOURCE_BACKFILL,
        )
        self.assertEqual(CompanyAlias.objects.count(), 1)
        company.delete()
        self.assertEqual(CompanyAlias.objects.count(), 0)


class TestCompanyFindByAlias(TestCase):
    def test_empty_input_returns_none(self):
        self.assertIsNone(Company.find_by_alias(""))
        self.assertIsNone(Company.find_by_alias(None))
        # All-punctuation slugs to empty
        self.assertIsNone(Company.find_by_alias("!!!"))

    def test_no_match_returns_none(self):
        Company.objects.create(name="Acme")
        # No alias row exists yet.
        self.assertIsNone(Company.find_by_alias("Acme"))

    def test_exact_slug_hit(self):
        company = Company.objects.create(name="Acme Corp")
        CompanyAlias.objects.create(
            company=company, name="Acme Corp",
            name_slug=slug(strip_corp_suffix("Acme Corp")),
            source=CompanyAlias.SOURCE_BACKFILL,
        )
        self.assertEqual(Company.find_by_alias("Acme Corp"), company)

    def test_case_drift_collapses(self):
        company = Company.objects.create(name="Acme Corp")
        CompanyAlias.objects.create(
            company=company, name="Acme Corp",
            name_slug=slug(strip_corp_suffix("Acme Corp")),
            source=CompanyAlias.SOURCE_BACKFILL,
        )
        self.assertEqual(Company.find_by_alias("ACME CORP"), company)
        self.assertEqual(Company.find_by_alias("acme corp"), company)

    def test_suffix_variants_collapse(self):
        """The JP 1162/1164 Allstate regression — different legal-entity
        suffixes must resolve to the same Company once an alias exists."""
        company = Company.objects.create(name="Allstate Corporation")
        CompanyAlias.objects.create(
            company=company, name="Allstate Corporation",
            name_slug=slug(strip_corp_suffix("Allstate Corporation")),
            source=CompanyAlias.SOURCE_BACKFILL,
        )
        self.assertEqual(
            Company.find_by_alias("Allstate Insurance Company"), company
        )
        self.assertEqual(Company.find_by_alias("Allstate Inc"), company)
        self.assertEqual(Company.find_by_alias("Allstate"), company)


class TestExtractorCompanyResolution(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", password="x")
        self.scrape = Scrape.objects.create(
            url="https://example.com/job/1",
            status="extracting",
            created_by=self.user,
        )

    def _make_parsed(self, **overrides):
        defaults = dict(
            title="Senior Engineer",
            company_name="Acme Corp",
            company_display_name="Acme",
            description="Build things.",
            location="Remote",
            remote=True,
        )
        defaults.update(overrides)
        return ParsedJobData(**defaults)

    def test_creates_company_and_self_alias(self):
        extractor = JobPostExtractor()
        extractor.process_evaluation(self.scrape, self._make_parsed(), user=self.user)
        company = Company.objects.get(name="Acme Corp")
        alias = CompanyAlias.objects.get(company=company)
        self.assertEqual(alias.source, CompanyAlias.SOURCE_EXTRACTION)
        self.assertEqual(alias.name_slug, slug(strip_corp_suffix("Acme Corp")))

    def test_alias_hit_attaches_to_existing_company(self):
        """The JP 1162/1164 fix: a fresh extraction whose name slugs to
        an existing alias must attach rather than mint a new Company."""
        existing = Company.objects.create(name="Allstate Corporation")
        CompanyAlias.objects.create(
            company=existing, name="Allstate Corporation",
            name_slug=slug(strip_corp_suffix("Allstate Corporation")),
            source=CompanyAlias.SOURCE_BACKFILL,
        )
        parsed = self._make_parsed(company_name="Allstate Insurance Company")
        extractor = JobPostExtractor()
        extractor.process_evaluation(self.scrape, parsed, user=self.user)
        self.assertEqual(Company.objects.filter(name__icontains="allstate").count(), 1)
        self.scrape.refresh_from_db()
        self.assertEqual(self.scrape.company_id, existing.id)

    def test_existing_literal_name_still_attaches(self):
        """Rollout-window fallback: a Company that exists in the DB but
        does NOT yet have an alias row must still resolve via the
        literal-name get_or_create path. Without this, the pre-backfill
        window would mint duplicate Companies."""
        existing = Company.objects.create(name="Acme Corp")
        parsed = self._make_parsed(company_name="Acme Corp")
        extractor = JobPostExtractor()
        extractor.process_evaluation(self.scrape, parsed, user=self.user)
        self.assertEqual(Company.objects.filter(name="Acme Corp").count(), 1)
        self.scrape.refresh_from_db()
        self.assertEqual(self.scrape.company_id, existing.id)

    def test_no_company_suggestions_when_alias_hit(self):
        """The fuzzy-suggestions path is presentation-only and runs
        only when a NEW Company is minted. An exact alias hit must
        not pollute scrape.company_suggestions."""
        existing = Company.objects.create(name="Acme Corp")
        CompanyAlias.objects.create(
            company=existing, name="Acme Corp",
            name_slug=slug(strip_corp_suffix("Acme Corp")),
            source=CompanyAlias.SOURCE_BACKFILL,
        )
        parsed = self._make_parsed(company_name="ACME CORP")
        extractor = JobPostExtractor()
        extractor.process_evaluation(self.scrape, parsed, user=self.user)
        self.scrape.refresh_from_db()
        self.assertIsNone(self.scrape.company_suggestions)

    def test_company_suggestions_stashed_on_new_mint(self):
        """Brand-new Company mint with a fuzzy-similar existing alias
        in the table: top-3 suggestions land on the scrape.

        Important: the seed names must NOT slug down to the incoming
        name's slug after corp-suffix strip, otherwise
        ``Company.find_by_alias`` would auto-attach and the suggestion
        path is skipped (which is the whole point of the alias gate).
        """
        # Seed several Companies whose slugs are distinct from
        # slug(strip_corp_suffix("Acme-Z Inc")) = "acme-z". All of
        # these slug to something other than "acme-z" so no exact
        # match short-circuits the suggestion path.
        seeds = [
            ("Acme-Z Software", "acme-z-software"),
            ("Acme-Z Robotics", "acme-z-robotics"),
            ("Acme-Z Studios", "acme-z-studios"),
            ("Globex", "globex"),
        ]
        for name, expected_slug in seeds:
            self.assertEqual(slug(strip_corp_suffix(name)), expected_slug)
            c = Company.objects.create(name=name)
            CompanyAlias.objects.create(
                company=c, name=name, name_slug=expected_slug,
                source=CompanyAlias.SOURCE_BACKFILL,
            )
        parsed = self._make_parsed(company_name="Acme-Z Inc")
        extractor = JobPostExtractor()
        extractor.process_evaluation(self.scrape, parsed, user=self.user)
        self.scrape.refresh_from_db()
        # The stashed payload is a list of up to 3 dicts; the parse
        # minted a new Company (Acme-Z Inc) because no exact slug hit
        # fired — so the suggestion block ran and stashed candidates.
        self.assertIsNotNone(self.scrape.company_suggestions)
        self.assertLessEqual(len(self.scrape.company_suggestions), 3)
        for suggestion in self.scrape.company_suggestions:
            self.assertIn("company_id", suggestion)
            self.assertIn("name", suggestion)
            self.assertIn("similarity", suggestion)


class TestCompanyMergeIntoEndpoint(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.staff = User.objects.create_user(
            username="staffy", password="x", is_staff=True,
        )
        self.user = User.objects.create_user(username="rando", password="x")
        self.source = Company.objects.create(name="Allstate Insurance Company")
        self.target = Company.objects.create(name="Allstate Corporation")
        # Seed aliases on both sides.
        CompanyAlias.objects.create(
            company=self.source, name="Allstate Insurance Company",
            name_slug="allstate-insurance",
            source=CompanyAlias.SOURCE_BACKFILL,
        )
        CompanyAlias.objects.create(
            company=self.target, name="Allstate Corporation",
            name_slug="allstate",
            source=CompanyAlias.SOURCE_BACKFILL,
        )
        # One JobPost + one Scrape + one JobApplication on the source side.
        self.jp = JobPost.objects.create(
            title="Engineer", company=self.source, link="https://example/jp1",
            created_by=self.user,
        )
        self.scrape_obj = Scrape.objects.create(
            url="https://example/jp1", company=self.source,
            created_by=self.user,
        )
        self.app = JobApplication.objects.create(
            job_post=self.jp, company=self.source, user=self.user,
        )

    def test_non_staff_forbidden(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(
            f"/api/v1/companies/{self.source.id}/merge-into/",
            data={"target_id": self.target.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)
        # No mutations occurred.
        self.assertTrue(Company.objects.filter(pk=self.source.id).exists())

    def test_missing_target_id_returns_400(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.post(
            f"/api/v1/companies/{self.source.id}/merge-into/",
            data={},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_self_merge_rejected(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.post(
            f"/api/v1/companies/{self.source.id}/merge-into/",
            data={"target_id": self.source.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_target_missing_returns_404(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.post(
            f"/api/v1/companies/{self.source.id}/merge-into/",
            data={"target_id": 999999},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_source_missing_returns_404(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.post(
            "/api/v1/companies/999999/merge-into/",
            data={"target_id": self.target.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_merge_moves_fks_and_deletes_source(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.post(
            f"/api/v1/companies/{self.source.id}/merge-into/",
            data={"target_id": self.target.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        # Source is gone.
        self.assertFalse(Company.objects.filter(pk=self.source.id).exists())
        # All FKs now point at target.
        self.jp.refresh_from_db()
        self.scrape_obj.refresh_from_db()
        self.app.refresh_from_db()
        self.assertEqual(self.jp.company_id, self.target.id)
        self.assertEqual(self.scrape_obj.company_id, self.target.id)
        self.assertEqual(self.app.company_id, self.target.id)
        # Alias moved over (no name_slug collision).
        self.assertTrue(
            CompanyAlias.objects.filter(
                company=self.target, name_slug="allstate-insurance"
            ).exists()
        )

    def test_merge_records_duplicate_annotation(self):
        self.client.force_authenticate(user=self.staff)
        before = DuplicateAnnotation.objects.count()
        self.client.post(
            f"/api/v1/companies/{self.source.id}/merge-into/",
            data={"target_id": self.target.id},
            format="json",
        )
        after = DuplicateAnnotation.objects.count()
        self.assertEqual(after - before, 1)
        ann = DuplicateAnnotation.objects.order_by("-id").first()
        self.assertEqual(ann.action, "company_merge")
        self.assertEqual(ann.signal_state["target_company_id"], self.target.id)
        self.assertEqual(ann.signal_state["source_company_name"], "Allstate Insurance Company")
        self.assertEqual(ann.signal_state["moved_jobpost_count"], 1)
        self.assertEqual(ann.set_by_id, self.staff.id)

    # NOTE: there is no "two-rows-same-slug" collision test because the
    # global UNIQUE constraint on CompanyAlias.name_slug makes the
    # state impossible to construct without dropping the constraint.
    # The defensive collision-drop logic in merge_into remains as
    # forward-safe code in case the constraint is ever scoped to
    # per-Company; if so, add a test here that seeds two rows with
    # the same (company_id, name_slug) pair and asserts the source
    # row is deleted.

    def test_merge_no_jobposts_skips_annotation(self):
        """Companies with zero linked JobPosts can still be merged
        (typo-only minting), but the audit annotation is skipped
        because ``DuplicateAnnotation.from_jp`` is non-nullable."""
        # Wipe the source-side JP first.
        self.app.delete()
        self.jp.delete()
        self.scrape_obj.delete()
        self.client.force_authenticate(user=self.staff)
        before = DuplicateAnnotation.objects.count()
        resp = self.client.post(
            f"/api/v1/companies/{self.source.id}/merge-into/",
            data={"target_id": self.target.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        # Source company gone. No annotation row written.
        self.assertFalse(Company.objects.filter(pk=self.source.id).exists())
        self.assertEqual(DuplicateAnnotation.objects.count(), before)
