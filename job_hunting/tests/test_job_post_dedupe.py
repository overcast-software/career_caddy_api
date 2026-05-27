from django.test import TestCase
from django.contrib.auth import get_user_model

from job_hunting.models import Company, JobPost, ScrapeProfile
from job_hunting.models.job_post_dedupe import (
    _profile_url_rewrites_for_host,
    canonicalize_link,
    find_apply_url_matches,
    find_duplicate,
    fingerprint,
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

    def test_apply_url_forward_match_decides_duplicate(self):
        """Jobboard JP exists with apply_url pointing at the ATS direct
        URL; later a JP for that ATS URL gets created. find_duplicate
        must return the jobboard JP. (The jp 2882/2923 case.)"""
        jobboard = JobPost.objects.create(
            title="Senior Security Engineer",
            company=self.company,
            link="https://wellfound.com/jobs/4250970-senior-security-engineer",
            apply_url="https://www.pindrop.com/careers/senior-security-engineer/?gh_jid=7943713",
            created_by=self.user,
        )
        candidate = JobPost(
            title="Senior Security Engineer",
            company=self.company,
            link="https://www.pindrop.com/careers/senior-security-engineer/?gh_jid=7943713",
        )
        candidate.canonical_link = canonicalize_link(candidate.link)
        self.assertEqual(find_duplicate(candidate), jobboard)

    def test_apply_url_reverse_match_decides_duplicate(self):
        """Direct-ATS JP exists; later a jobboard JP with apply_url
        pointing back at the ATS link gets created."""
        ats_direct = JobPost.objects.create(
            title="Senior Security Engineer",
            company=self.company,
            link="https://www.pindrop.com/careers/senior-security-engineer/?gh_jid=7943713",
            created_by=self.user,
        )
        candidate = JobPost(
            title="Senior Security Engineer",
            company=self.company,
            link="https://wellfound.com/jobs/4250970",
            apply_url="https://www.pindrop.com/careers/senior-security-engineer/?gh_jid=7943713",
        )
        candidate.canonical_link = canonicalize_link(candidate.link)
        self.assertEqual(find_duplicate(candidate), ats_direct)

    def test_apply_url_match_walks_canonical_chain(self):
        """When the apply-url-matched post is itself flagged as a
        duplicate of an older post, the older canonical ancestor is
        returned (matching the canonical_link/fingerprint stages)."""
        other_company = Company.objects.create(name="Older Co")
        ancestor = JobPost.objects.create(
            title="Old listing",
            company=other_company,
            link="https://old.example/job/1",
            created_by=self.user,
        )
        JobPost.objects.create(
            title="Senior Security Engineer",
            company=self.company,
            link="https://example.com/board/abc",
            apply_url="https://www.pindrop.com/careers/senior-security-engineer/",
            duplicate_of=ancestor,
            created_by=self.user,
        )
        candidate = JobPost(
            title="Senior Security Engineer",
            company=self.company,
            link="https://www.pindrop.com/careers/senior-security-engineer/",
        )
        candidate.canonical_link = canonicalize_link(candidate.link)
        self.assertEqual(find_duplicate(candidate), ancestor)

    def test_apply_url_stage_runs_after_canonical_link(self):
        """If both canonical_link and apply_url reciprocity match
        different existing posts, canonical_link wins (it's the
        deterministic same-link signal). Pins stage ordering."""
        canonical_match = JobPost.objects.create(
            title="Same listing reposted",
            company=self.company,
            link="https://example.com/job/9",
            created_by=self.user,
        )
        # Older JP that ALSO matches via apply_url reciprocity but
        # should be passed over because canonical_link already matched.
        JobPost.objects.create(
            title="Cross-platform earlier",
            company=self.company,
            link="https://wellfound.com/jobs/cross",
            apply_url="https://example.com/job/9?utm_source=x",
            created_by=self.user,
        )
        candidate = JobPost(
            title="Repost",
            company=self.company,
            link="https://example.com/job/9?utm_source=x",
        )
        candidate.canonical_link = canonicalize_link(candidate.link)
        self.assertEqual(find_duplicate(candidate), canonical_match)


class TestFindApplyUrlMatches(TestCase):
    """Unit tests for the shared apply_url reciprocity primitive."""

    def setUp(self):
        self.user = User.objects.create_user(username="apply", password="pass")
        self.company = Company.objects.create(name="Acme")

    def test_forward_match(self):
        """existing.apply_url == incoming.link."""
        existing = JobPost.objects.create(
            title="Eng",
            company=self.company,
            link="https://a.example/job",
            apply_url="https://ats.example/job/1",
            created_by=self.user,
        )
        candidate = JobPost(
            title="Eng",
            company=self.company,
            link="https://ats.example/job/1",
        )
        candidate.canonical_link = candidate.link
        self.assertIn(existing, list(find_apply_url_matches(candidate)))

    def test_reverse_match(self):
        """incoming.apply_url == existing.link."""
        existing = JobPost.objects.create(
            title="Eng",
            company=self.company,
            link="https://ats.example/job/1",
            created_by=self.user,
        )
        candidate = JobPost(
            title="Eng",
            company=self.company,
            link="https://a.example/job",
            apply_url="https://ats.example/job/1",
        )
        candidate.canonical_link = candidate.link
        self.assertIn(existing, list(find_apply_url_matches(candidate)))

    def test_matches_via_canonical_link_too(self):
        """existing.apply_url matches incoming.canonical_link even when
        incoming.link still carries tracking params."""
        existing = JobPost.objects.create(
            title="Eng",
            company=self.company,
            link="https://a.example/job",
            apply_url="https://ats.example/job/1",
            created_by=self.user,
        )
        candidate = JobPost(
            title="Eng",
            company=self.company,
            link="https://ats.example/job/1?utm_source=x",
        )
        candidate.canonical_link = canonicalize_link(candidate.link)
        self.assertIn(existing, list(find_apply_url_matches(candidate)))

    def test_no_links_or_apply_url_returns_empty(self):
        candidate = JobPost(title="No urls")
        self.assertFalse(find_apply_url_matches(candidate).exists())

    def test_no_signal_returns_empty(self):
        """Posts exist but none of their apply_url / link fields reciprocate."""
        JobPost.objects.create(
            title="Unrelated",
            company=self.company,
            link="https://other.example/job",
            created_by=self.user,
        )
        candidate = JobPost(
            title="Eng",
            company=self.company,
            link="https://nope.example/job",
            apply_url="https://no-match.example/ats",
        )
        candidate.canonical_link = candidate.link
        self.assertFalse(find_apply_url_matches(candidate).exists())

    def test_respects_base_qs(self):
        """When a base_qs filters out potential matches, they don't surface."""
        other = User.objects.create_user(username="other", password="pass")
        in_scope = JobPost.objects.create(
            title="In",
            company=self.company,
            link="https://a.example/job",
            apply_url="https://ats.example/job/1",
            created_by=self.user,
        )
        out_of_scope = JobPost.objects.create(
            title="Out",
            company=self.company,
            link="https://b.example/job",
            apply_url="https://ats.example/job/1",
            created_by=other,
        )
        candidate = JobPost(
            title="Cand",
            company=self.company,
            link="https://ats.example/job/1",
        )
        candidate.canonical_link = candidate.link
        results = list(find_apply_url_matches(
            candidate, base_qs=JobPost.objects.filter(created_by=self.user)
        ))
        self.assertIn(in_scope, results)
        self.assertNotIn(out_of_scope, results)


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
