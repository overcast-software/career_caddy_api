from django.test import TestCase
from django.contrib.auth import get_user_model

from job_hunting.models import Company, JobPost
from job_hunting.models.job_post_dedupe import (
    canonicalize_link,
    fingerprint,
    find_duplicate,
)

User = get_user_model()


class TestCanonicalizeLink(TestCase):
    def test_returns_none_for_falsy(self):
        self.assertIsNone(canonicalize_link(None))
        self.assertIsNone(canonicalize_link(""))

    def test_strips_utm_params(self):
        got = canonicalize_link(
            "https://example.com/jobs/42?utm_source=linkedin&utm_medium=email&ref=keep"
        )
        self.assertIn("ref=keep", got)
        self.assertNotIn("utm_source", got)
        self.assertNotIn("utm_medium", got)

    def test_strips_fragment(self):
        got = canonicalize_link("https://example.com/jobs/42#apply")
        self.assertNotIn("#apply", got)

    def test_leaves_ziprecruiter_ekm_alone(self):
        # Opaque token IS the identifier; no tracking params to strip.
        url = "https://www.ziprecruiter.com/ekm/AAFu8_x25OYUUjsmCpD2H7FIn"
        self.assertEqual(canonicalize_link(url), url)


class TestFingerprint(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme")

    def test_none_without_company(self):
        jp = JobPost(title="Engineer")
        self.assertIsNone(fingerprint(jp))

    def test_none_without_title(self):
        jp = JobPost(company=self.company)
        self.assertIsNone(fingerprint(jp))

    def test_stable_across_case_and_whitespace(self):
        a = JobPost(company=self.company, title=" Software Engineer ", location="Redmond, WA")
        b = JobPost(company=self.company, title="software  engineer", location="redmond, wa")
        self.assertEqual(fingerprint(a), fingerprint(b))

    def test_description_tweak_does_not_change_hash(self):
        a = JobPost(company=self.company, title="Dev", location="NYC", description="A" * 500)
        b = JobPost(company=self.company, title="Dev", location="NYC", description="B" * 500)
        self.assertEqual(fingerprint(a), fingerprint(b))

    def test_different_company_different_hash(self):
        other = Company.objects.create(name="Beta")
        a = JobPost(company=self.company, title="Dev", location="NYC")
        b = JobPost(company=other, title="Dev", location="NYC")
        self.assertNotEqual(fingerprint(a), fingerprint(b))


class TestFindDuplicate(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="dup", password="pass")
        self.company = Company.objects.create(name="Acme")
        self.original = JobPost.objects.create(
            title="Software Engineer III",
            company=self.company,
            location="Redmond, WA",
            link="https://www.ziprecruiter.com/ekm/TOKEN-A",
            created_by=self.user,
        )

    def test_reposted_ziprecruiter_matches_via_fingerprint(self):
        # Different /ekm/ token, same role — the 1184/1206 case.
        candidate = JobPost(
            title="software engineer iii",
            company=self.company,
            location="Redmond, WA",
            link="https://www.ziprecruiter.com/ekm/TOKEN-B",
        )
        candidate.content_fingerprint = fingerprint(candidate)
        candidate.canonical_link = canonicalize_link(candidate.link)
        hit = find_duplicate(candidate)
        self.assertEqual(hit, self.original)

    def test_different_location_is_not_duplicate(self):
        candidate = JobPost(
            title="Software Engineer III",
            company=self.company,
            location="Austin, TX",
        )
        candidate.content_fingerprint = fingerprint(candidate)
        self.assertIsNone(find_duplicate(candidate))

    def test_null_fingerprint_skips_dedupe(self):
        candidate = JobPost(title="Dev")  # no company → null fingerprint
        self.assertIsNone(find_duplicate(candidate))

    def test_canonical_link_match(self):
        # Same canonicalized URL via utm variance.
        JobPost.objects.create(
            title="Other",
            company=self.company,
            link="https://example.com/job/9",
            created_by=self.user,
        )
        candidate = JobPost(
            title="Unrelated",  # title differs → fingerprint can't match
            company=Company.objects.create(name="Zed"),
            link="https://example.com/job/9?utm_source=x",
        )
        candidate.canonical_link = canonicalize_link(candidate.link)
        candidate.content_fingerprint = fingerprint(candidate)
        hit = find_duplicate(candidate)
        self.assertIsNotNone(hit)


class TestJobPostSavePopulatesDedupeFields(TestCase):
    def test_save_fills_canonical_link_and_fingerprint(self):
        company = Company.objects.create(name="Acme")
        jp = JobPost.objects.create(
            title="Dev",
            company=company,
            location="NYC",
            link="https://example.com/job/1?utm_source=x",
        )
        jp.refresh_from_db()
        self.assertEqual(jp.canonical_link, "https://example.com/job/1")
        self.assertIsNotNone(jp.content_fingerprint)
        self.assertEqual(len(jp.content_fingerprint), 40)
