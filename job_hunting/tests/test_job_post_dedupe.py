from django.test import TestCase
from django.contrib.auth import get_user_model

from job_hunting.models import Company, JobPost, ScrapeProfile
from job_hunting.models.job_post_dedupe import (
    _profile_url_rewrites_for_host,
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

    def test_strips_ziprecruiter_clickout_tokens(self):
        # lk / lvk / tsid are ZipRecruiter clickout referral params.
        # Same job from list page (lk) vs reposted card (lvk) used to
        # produce two JobPosts because dedup matched on canonical_link.
        got = canonicalize_link(
            "https://www.ziprecruiter.com/jobs/altus-llc/software-developer-c-remote"
            "?lk=ABC&lvk=XYZ&tsid=12345&keep=1"
        )
        self.assertNotIn("lk=", got)
        self.assertNotIn("lvk=", got)
        self.assertNotIn("tsid=", got)
        self.assertIn("keep=1", got)


class TestCanonicalizeLinkProfileRewrites(TestCase):
    """Host-scoped path rewrites from ScrapeProfile.url_rewrites should
    collapse URL variants (e.g. LinkedIn /comm/jobs/view/ vs /jobs/view/)
    onto a single canonical_link so dedup recognises them as the same job.
    """

    def setUp(self):
        _profile_url_rewrites_for_host.cache_clear()
        ScrapeProfile.objects.update_or_create(
            hostname="linkedin.com",
            defaults={"url_rewrites": [{
                "match": r"^https?://www\.linkedin\.com/comm/jobs/view/",
                "rewrite": "https://www.linkedin.com/jobs/view/",
            }]},
        )

    def tearDown(self):
        _profile_url_rewrites_for_host.cache_clear()

    def test_linkedin_comm_path_rewritten_to_canonical(self):
        comm = "https://www.linkedin.com/comm/jobs/view/4409035385/"
        canonical = "https://www.linkedin.com/jobs/view/4409035385/"
        self.assertEqual(canonicalize_link(comm), canonical)

    def test_linkedin_canonical_path_unchanged(self):
        url = "https://www.linkedin.com/jobs/view/4409035385/"
        self.assertEqual(canonicalize_link(url), url)

    def test_canonicalize_strips_tracking_after_path_rewrite(self):
        comm = (
            "https://www.linkedin.com/comm/jobs/view/4409035385/"
            "?trk=email&utm_source=foo"
        )
        got = canonicalize_link(comm)
        self.assertIn("/jobs/view/4409035385/", got)
        self.assertNotIn("/comm/", got)
        self.assertNotIn("trk=", got)
        self.assertNotIn("utm_", got)

    def test_unknown_host_passes_through(self):
        url = "https://no-profile.example/jobs/1?utm_source=x"
        got = canonicalize_link(url)
        self.assertNotIn("utm_source", got)
        self.assertIn("/jobs/1", got)

    def test_ziprecruiter_strips_8hex_slug_tokens(self):
        """ZipRecruiter listing URLs include a -<8hex> token on both the
        company-slug and the job-slug. The canonical clean URL drops
        both. Without this rule, /jobs/altus-llc-2287a4f5/sw-eng-2ffd5d4c
        and /jobs/altus-llc/sw-eng resolve to distinct canonical_links."""
        _profile_url_rewrites_for_host.cache_clear()
        ScrapeProfile.objects.update_or_create(
            hostname="ziprecruiter.com",
            defaults={"url_rewrites": [{
                "match": r"/jobs/([^/]+?)-([a-f0-9]{8})/([^/?#]+?)-([a-f0-9]{8})(?=[/?#]|$)",
                "rewrite": r"/jobs/\1/\3",
            }]},
        )
        tokenized = (
            "https://www.ziprecruiter.com"
            "/jobs/altus-llc-2287a4f5/software-developer-c-remote-2ffd5d4c"
            "?lk=ABC&tsid=XYZ"
        )
        clean = (
            "https://www.ziprecruiter.com"
            "/jobs/altus-llc/software-developer-c-remote"
            "?lvk=ABC"
        )
        self.assertEqual(canonicalize_link(tokenized), canonicalize_link(clean))

    def test_ziprecruiter_no_token_unchanged(self):
        """Real path without the -<8hex> suffix must not be rewritten."""
        _profile_url_rewrites_for_host.cache_clear()
        ScrapeProfile.objects.update_or_create(
            hostname="ziprecruiter.com",
            defaults={"url_rewrites": [{
                "match": r"/jobs/([^/]+?)-([a-f0-9]{8})/([^/?#]+?)-([a-f0-9]{8})(?=[/?#]|$)",
                "rewrite": r"/jobs/\1/\3",
            }]},
        )
        url = "https://www.ziprecruiter.com/jobs/altus-llc/software-developer-c-remote"
        self.assertEqual(canonicalize_link(url), url)

    def test_reads_legacy_css_selectors_url_rewrites(self):
        """Some profiles authored rewrites inside the css_selectors blob
        rather than the top-level url_rewrites column."""
        _profile_url_rewrites_for_host.cache_clear()
        ScrapeProfile.objects.create(
            hostname="legacy.example",
            css_selectors={
                "url_rewrites": [{
                    "match": r"^https?://legacy\.example/old/",
                    "rewrite": "https://legacy.example/new/",
                }],
            },
        )
        got = canonicalize_link("https://legacy.example/old/job/1")
        self.assertEqual(got, "https://legacy.example/new/job/1")


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
