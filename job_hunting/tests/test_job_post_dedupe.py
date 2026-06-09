from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from job_hunting.models import Company, JobPost, ScrapeProfile
from job_hunting.models.job_post_dedupe import (
    _profile_url_rewrites_for_host,
    bump_last_seen,
    canonicalize_link,
    find_apply_url_matches,
    find_duplicate,
    fingerprint,
    normalized_fingerprint,
    strip_url_trailing_junk,
)

User = get_user_model()


class TestStripUrlTrailingJunk(TestCase):
    """Regression for the 2026-05-27 hiring.cafe JP 2981 incident — LLM
    URL extractor in cc_auto captured the closing ``"`` from the HTML
    attribute, the api stored it verbatim, the frontend URL-encoded it
    to ``%22`` in the apply href, and hiring.cafe 404'd."""

    def test_falsy_passes_through(self):
        self.assertIsNone(strip_url_trailing_junk(None))
        self.assertEqual(strip_url_trailing_junk(""), "")

    def test_strips_trailing_double_quote(self):
        self.assertEqual(
            strip_url_trailing_junk('https://hiring.cafe/job/5fsbbgitg82ev1ar"'),
            "https://hiring.cafe/job/5fsbbgitg82ev1ar",
        )

    def test_strips_trailing_single_quote(self):
        self.assertEqual(
            strip_url_trailing_junk("https://example.com/jobs/42'"),
            "https://example.com/jobs/42",
        )

    def test_strips_trailing_brackets_and_parens(self):
        for trail in (")", "]", "}", ">", "}}]>"):
            self.assertEqual(
                strip_url_trailing_junk(f"https://example.com/jobs/42{trail}"),
                "https://example.com/jobs/42",
                msg=f"trailing {trail!r}",
            )

    def test_strips_trailing_whitespace_and_comma(self):
        self.assertEqual(
            strip_url_trailing_junk("https://example.com/jobs/42,\n"),
            "https://example.com/jobs/42",
        )

    def test_leaves_clean_url_alone(self):
        url = "https://example.com/jobs/42?utm=foo&id=1"
        self.assertEqual(strip_url_trailing_junk(url), url)

    def test_preserves_internal_punctuation(self):
        # Only the TRAILING run is stripped — quotes mid-URL stay (they'd
        # never be valid but we don't want to mangle stored data more
        # than necessary).
        self.assertEqual(
            strip_url_trailing_junk('https://example.com/a"b/c'),
            'https://example.com/a"b/c',
        )

    def test_canonicalize_link_strips_trailing_junk(self):
        # canonicalize_link must invoke the strip as its first step so
        # downstream urlparse/dedup never sees the slop.
        self.assertEqual(
            canonicalize_link('https://example.com/jobs/42"'),
            "https://example.com/jobs/42",
        )


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

    def test_strips_trailing_slash_on_non_root_path(self):
        # Regression for JP 715 / JP 2963 LinkedIn pair: both rows had
        # the same job id but one canonical_link ended with `/` and the
        # other didn't, breaking stage-1 exact-match dedup.
        self.assertEqual(
            canonicalize_link("https://www.linkedin.com/jobs/view/4335567500/"),
            "https://www.linkedin.com/jobs/view/4335567500",
        )

    def test_two_urls_differing_only_by_trailing_slash_collapse(self):
        with_slash = canonicalize_link("https://example.com/jobs/42/")
        without_slash = canonicalize_link("https://example.com/jobs/42")
        self.assertEqual(with_slash, without_slash)

    def test_preserves_root_path(self):
        # Bare `/` is the path delimiter — stripping it would produce
        # `https://example.com` (no path) which technically parses but
        # round-trips inconsistently across libraries.
        self.assertEqual(
            canonicalize_link("https://example.com/"),
            "https://example.com/",
        )

    def test_strips_repeated_trailing_slashes(self):
        self.assertEqual(
            canonicalize_link("https://example.com/jobs/42///"),
            "https://example.com/jobs/42",
        )

    def test_trailing_slash_before_query_strips(self):
        self.assertEqual(
            canonicalize_link("https://example.com/jobs/42/?id=1"),
            "https://example.com/jobs/42?id=1",
        )

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
        # Trailing slash is normalized away in canonicalize_link (the
        # 2026-05-27 JP 715 / JP 2963 fix). The rewrite still collapses
        # /comm/jobs/view/ → /jobs/view/, then the slash strip applies.
        comm = "https://www.linkedin.com/comm/jobs/view/4409035385/"
        canonical = "https://www.linkedin.com/jobs/view/4409035385"
        self.assertEqual(canonicalize_link(comm), canonical)

    def test_linkedin_canonical_path_normalized_without_slash(self):
        # The canonical form post-2026-05-27 has no trailing slash. Calling
        # canonicalize_link is idempotent on the no-slash form.
        with_slash = "https://www.linkedin.com/jobs/view/4409035385/"
        without_slash = "https://www.linkedin.com/jobs/view/4409035385"
        self.assertEqual(canonicalize_link(with_slash), without_slash)
        self.assertEqual(canonicalize_link(without_slash), without_slash)

    def test_canonicalize_strips_tracking_after_path_rewrite(self):
        comm = (
            "https://www.linkedin.com/comm/jobs/view/4409035385/"
            "?trk=email&utm_source=foo"
        )
        got = canonicalize_link(comm)
        self.assertIn("/jobs/view/4409035385", got)
        self.assertNotIn("/comm/", got)
        self.assertNotIn("trk=", got)
        self.assertNotIn("utm_", got)
        # Trailing slash must be stripped even when the path has a query.
        self.assertNotIn("/4409035385/?", got)

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


class TestNormalizedFingerprint(TestCase):
    """Phase B fingerprint — slug-folded title + location.

    Catches the punctuation-drift twins (en-dash vs hyphen vs minus,
    smart quote vs ASCII quote, NFKC-foldable codepoints) that the
    case+whitespace fold in ``fingerprint`` cannot see.
    """

    def setUp(self):
        self.company = Company.objects.create(name="Allstate")

    def test_none_without_company(self):
        jp = JobPost(title="Engineer")
        self.assertIsNone(normalized_fingerprint(jp))

    def test_none_without_title(self):
        jp = JobPost(company=self.company)
        self.assertIsNone(normalized_fingerprint(jp))

    def test_en_dash_and_hyphen_collapse(self):
        """JP 1329 vs JP 3323 regression: U+002D hyphen-minus and
        U+2013 en-dash in titles collapse to the same hash."""
        hyphen = JobPost(
            company=self.company,
            title="Software Engineer - Product Security",
            location="Northbrook, IL",
        )
        en_dash = JobPost(
            company=self.company,
            title="Software Engineer – Product Security",
            location="Northbrook, IL",
        )
        self.assertEqual(
            normalized_fingerprint(hyphen),
            normalized_fingerprint(en_dash),
        )

    def test_em_dash_and_minus_sign_also_collapse(self):
        """The whole unicode dash family folds — em-dash and the math
        minus sign aren't just hypothetical; LinkedIn renders the
        math minus in some job titles."""
        em_dash = JobPost(
            company=self.company,
            title="Software Engineer — Product Security",
            location="NYC",
        )
        minus = JobPost(
            company=self.company,
            title="Software Engineer − Product Security",
            location="NYC",
        )
        hyphen = JobPost(
            company=self.company,
            title="Software Engineer - Product Security",
            location="NYC",
        )
        self.assertEqual(
            normalized_fingerprint(em_dash),
            normalized_fingerprint(hyphen),
        )
        self.assertEqual(
            normalized_fingerprint(minus),
            normalized_fingerprint(hyphen),
        )

    def test_smart_quotes_collapse(self):
        """Curly single-quote vs ASCII apostrophe."""
        smart = JobPost(
            company=self.company,
            title="Driver’s License Manager",
            location="NYC",
        )
        ascii_q = JobPost(
            company=self.company,
            title="Driver's License Manager",
            location="NYC",
        )
        self.assertEqual(
            normalized_fingerprint(smart),
            normalized_fingerprint(ascii_q),
        )

    def test_case_and_whitespace_collapse(self):
        a = JobPost(
            company=self.company,
            title=" Software Engineer ",
            location="Redmond, WA",
        )
        b = JobPost(
            company=self.company,
            title="software  engineer",
            location="redmond, wa",
        )
        self.assertEqual(
            normalized_fingerprint(a),
            normalized_fingerprint(b),
        )

    def test_different_company_different_hash(self):
        other = Company.objects.create(name="Beta")
        a = JobPost(company=self.company, title="Dev", location="NYC")
        b = JobPost(company=other, title="Dev", location="NYC")
        self.assertNotEqual(
            normalized_fingerprint(a),
            normalized_fingerprint(b),
        )

    def test_substantive_title_difference_different_hash(self):
        """Different words (not just punctuation noise) must produce
        different hashes — the slug fold doesn't over-collapse."""
        a = JobPost(
            company=self.company,
            title="Software Engineer",
            location="NYC",
        )
        b = JobPost(
            company=self.company,
            title="Senior Software Engineer",
            location="NYC",
        )
        self.assertNotEqual(
            normalized_fingerprint(a),
            normalized_fingerprint(b),
        )

    def test_returns_40_char_hex(self):
        jp = JobPost(
            company=self.company, title="Dev", location="NYC"
        )
        fp = normalized_fingerprint(jp)
        self.assertEqual(len(fp), 40)
        # sha1 hex — all chars in [0-9a-f].
        self.assertTrue(all(c in "0123456789abcdef" for c in fp))


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
        # Phase B: normalized_fingerprint also populated on every save.
        self.assertIsNotNone(jp.normalized_fingerprint)
        self.assertEqual(len(jp.normalized_fingerprint), 40)

    def test_save_skips_normalized_fingerprint_when_company_missing(self):
        """Null-skip semantics mirror ``fingerprint`` — a stub post
        with no company gets a null normalized_fingerprint, NOT a
        crash."""
        jp = JobPost.objects.create(
            title="Orphan",
            link="https://example.com/orphan",
        )
        jp.refresh_from_db()
        self.assertIsNone(jp.normalized_fingerprint)
        self.assertIsNone(jp.content_fingerprint)


class TestFindDuplicateNormalizedFingerprint(TestCase):
    """Phase B widened ``find_duplicate`` fingerprint stage — matches
    by either ``content_fingerprint`` OR ``normalized_fingerprint``.

    Pin the JP 1329 / JP 3323 regression: same role, same company,
    title differs only by U+002D hyphen vs U+2013 en-dash. The case+
    whitespace fold in ``fingerprint`` produces different sha1s; the
    slug fold in ``normalized_fingerprint`` produces the same. The
    widened fingerprint stage finds the existing row via the new
    column when the legacy column misses.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="phaseb", password="pass")
        self.company = Company.objects.create(name="Allstate")

    def test_en_dash_vs_hyphen_dedupes_via_normalized_fingerprint(self):
        original = JobPost.objects.create(
            title="Software Engineer - Product Security",
            company=self.company,
            location="Northbrook, IL",
            link="https://linkedin.com/jobs/view/4405744429",
            created_by=self.user,
        )
        original.refresh_from_db()

        # Candidate uses the en-dash glyph. Different link so the
        # canonical_link stage misses; no apply_url so that stage
        # misses too. Only the fingerprint stage can fire — and only
        # the normalized column matches, the legacy column does not.
        candidate = JobPost(
            title="Software Engineer – Product Security",  # U+2013
            company=self.company,
            location="Northbrook, IL",
            link="https://allstate.jobs/job/23310874",
        )
        candidate.canonical_link = canonicalize_link(candidate.link)
        candidate.content_fingerprint = fingerprint(candidate)
        candidate.normalized_fingerprint = normalized_fingerprint(candidate)

        # Sanity-check the regression precondition: the legacy column
        # IS different (case+whitespace fold doesn't fold the en-dash);
        # the new column matches.
        self.assertNotEqual(
            original.content_fingerprint,
            candidate.content_fingerprint,
        )
        self.assertEqual(
            original.normalized_fingerprint,
            candidate.normalized_fingerprint,
        )

        hit = find_duplicate(candidate)
        self.assertEqual(hit, original)

    def test_legacy_content_fingerprint_still_dedupes(self):
        """Backward compat: when content_fingerprint matches but
        normalized_fingerprint differs (shouldn't happen in practice
        but defensible because the slug fold is a strict refinement),
        the legacy column still drives a dedupe hit. Tests the OR
        predicate from the candidate-only-has-legacy direction."""
        original = JobPost.objects.create(
            title="Senior Engineer",
            company=self.company,
            location="NYC",
            link="https://example.com/old",
            created_by=self.user,
        )
        original.refresh_from_db()

        candidate = JobPost(
            title="Senior Engineer",
            company=self.company,
            location="NYC",
            link="https://example.com/new",
        )
        candidate.canonical_link = canonicalize_link(candidate.link)
        candidate.content_fingerprint = fingerprint(candidate)
        # Force-null the new column on the candidate to simulate a
        # write path that hasn't been updated to populate it. The OR
        # predicate should still match via content_fingerprint.
        candidate.normalized_fingerprint = None
        self.assertEqual(find_duplicate(candidate), original)

    def test_both_null_skips_dedupe(self):
        """When both fingerprint columns are null on the candidate
        (e.g. stub post with no company), the fingerprint stage is
        skipped entirely — neither column drives a query."""
        candidate = JobPost(title="Orphan")
        candidate.content_fingerprint = None
        candidate.normalized_fingerprint = None
        self.assertIsNone(find_duplicate(candidate))


class TestComputeDuplicateCandidatesNormalizedFingerprint(TestCase):
    """Phase B added a ``normalized_fingerprint`` signal to
    ``compute_duplicate_candidates``. Surface it as a high-confidence
    reason code so the frontend duplicate-candidates panel can render
    the punctuation-drift twins the legacy ``fingerprint`` signal
    silently missed."""

    def setUp(self):
        from rest_framework.test import APIRequestFactory

        self.user = User.objects.create_user(
            username="cdcphaseb", password="pass"
        )
        self.company = Company.objects.create(name="Allstate")
        self.factory = APIRequestFactory()

    def _request(self):
        req = self.factory.get("/api/v1/job-posts/")
        req.user = self.user
        return req

    def test_normalized_fingerprint_signal_emitted(self):
        """En-dash vs hyphen pair: candidate is the en-dash version,
        existing post is the hyphen version. Signal must surface."""
        from job_hunting.api.serializers import compute_duplicate_candidates

        existing = JobPost.objects.create(
            title="Software Engineer - Product Security",
            company=self.company,
            location="Northbrook, IL",
            link="https://linkedin.com/jobs/view/4405744429",
            created_by=self.user,
        )
        existing.refresh_from_db()

        candidate = JobPost.objects.create(
            title="Software Engineer – Product Security",  # U+2013
            company=self.company,
            location="Northbrook, IL",
            link="https://allstate.jobs/job/23310874",
            created_by=self.user,
        )
        candidate.refresh_from_db()

        # Sanity: legacy column differs, new column matches.
        self.assertNotEqual(
            existing.content_fingerprint, candidate.content_fingerprint
        )
        self.assertEqual(
            existing.normalized_fingerprint,
            candidate.normalized_fingerprint,
        )

        candidates = compute_duplicate_candidates(candidate, self._request())
        self.assertEqual(len(candidates), 1)
        signals = candidates[0]._match_signals
        self.assertIn("normalized_fingerprint", signals)
        # Legacy fingerprint signal must NOT fire — the columns differ.
        self.assertNotIn("fingerprint", signals)
        # High-confidence (same tier as the existing fingerprint signal).
        self.assertEqual(candidates[0]._confidence, "high")

    def test_both_fingerprint_signals_stack_when_columns_agree(self):
        """When the legacy and new columns coincidentally agree (the
        common case — no exotic punctuation in either title), the
        candidate carries BOTH signal codes for the same hit. The
        ``_add`` helper stacks them by candidate id."""
        from job_hunting.api.serializers import compute_duplicate_candidates

        JobPost.objects.create(
            title="Plain Engineer",
            company=self.company,
            location="NYC",
            link="https://example.com/a",
            created_by=self.user,
        )
        candidate = JobPost.objects.create(
            title="Plain Engineer",
            company=self.company,
            location="NYC",
            link="https://example.com/b",
            created_by=self.user,
        )
        candidate.refresh_from_db()

        candidates = compute_duplicate_candidates(candidate, self._request())
        self.assertEqual(len(candidates), 1)
        signals = candidates[0]._match_signals
        self.assertIn("fingerprint", signals)
        self.assertIn("normalized_fingerprint", signals)


class TestRollingFingerprintWindow(TestCase):
    """Regression for the rolling-window dedupe enhancement.

    The 30-day fingerprint window previously queried ``created_at``,
    blunting cross-platform dedupe for long-tail roles. JP 1329
    (Allstate, 42 days old) was the canonical repro: a fresh capture
    of the same role from a different host failed to dedupe.

    These tests pin the contract: the window is now keyed on
    ``last_seen_at``, and any write-path resolve-to-existing decision
    must bump it.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="rolling", password="pass")
        self.company = Company.objects.create(name="Allstate")

    def _make_original(self, *, created_days_ago: int, last_seen_days_ago: int):
        now = timezone.now()
        original = JobPost.objects.create(
            title="Software Engineer Product Security",
            company=self.company,
            location="Northbrook, IL",
            link="https://linkedin.com/jobs/view/4405744429",
            created_by=self.user,
        )
        # Bypass auto_now_add by going through queryset update; refresh
        # afterwards so the in-memory instance matches.
        JobPost.objects.filter(pk=original.pk).update(
            created_at=now - timedelta(days=created_days_ago),
            last_seen_at=now - timedelta(days=last_seen_days_ago),
        )
        original.refresh_from_db()
        return original

    def _make_candidate(self):
        candidate = JobPost(
            title="software engineer product security",
            company=self.company,
            location="Northbrook, IL",
            link="https://allstate.jobs/job/23310874",
        )
        candidate.canonical_link = canonicalize_link(candidate.link)
        candidate.content_fingerprint = fingerprint(candidate)
        return candidate

    def test_old_post_kept_alive_by_recent_last_seen_matches(self):
        """JP 1329 case: created 42d ago but bumped 5d ago via a
        rescrape — must still dedupe under the rolling window."""
        original = self._make_original(
            created_days_ago=42, last_seen_days_ago=5
        )
        candidate = self._make_candidate()
        self.assertEqual(find_duplicate(candidate), original)

    def test_stale_post_with_old_last_seen_does_not_match(self):
        """Symmetric: when neither created_at nor last_seen_at is in
        the rolling window, fingerprint dedupe correctly returns None
        (the role is truly stale, not a long-tail repost)."""
        self._make_original(
            created_days_ago=120, last_seen_days_ago=120
        )
        candidate = self._make_candidate()
        # Strip canonical_link so we exercise the fingerprint stage
        # alone — the candidate's link is a different host so a
        # canonical_link match wouldn't fire anyway, but this makes
        # the test explicit.
        candidate.canonical_link = None
        self.assertIsNone(find_duplicate(candidate))

    def test_bump_last_seen_advances_the_column(self):
        """``bump_last_seen`` writes a fresh timestamp via
        update_fields and the persisted value reflects it."""
        original = self._make_original(
            created_days_ago=42, last_seen_days_ago=42
        )
        before = original.last_seen_at
        bump_last_seen(original)
        original.refresh_from_db()
        self.assertGreater(original.last_seen_at, before)

    def test_merge_empty_fields_bumps_last_seen(self):
        """The shared merge helper bumps last_seen_at on every call,
        even when no DEDUPE_BACKFILL_FIELDS need to be filled."""
        from job_hunting.lib.job_post_merge import (
            merge_empty_fields_from_attrs,
        )

        original = self._make_original(
            created_days_ago=42, last_seen_days_ago=42
        )
        before = original.last_seen_at
        # No-op merge: pass an empty attrs dict so no field is written
        # but the helper still bumps last_seen_at.
        written = merge_empty_fields_from_attrs(original, {})
        self.assertEqual(written, [])
        original.refresh_from_db()
        self.assertGreater(original.last_seen_at, before)

    def test_create_path_dedupe_via_fingerprint_bumps_existing(self):
        """Integration: a second JobPost.save() that gets routed
        through find_duplicate (here: simulating the merge call site
        in views/jobs.py) bumps the existing row's last_seen_at.

        Original is 42 days old by created_at but was kept alive 5
        days ago (the rolling-window enhancement's central use case
        — JP 1329 Allstate). The fingerprint dedupe stage finds it
        via the rolling last_seen_at predicate; the merge call site
        then bumps last_seen_at again to reflect this new sighting."""
        from job_hunting.lib.job_post_merge import (
            merge_empty_fields_from_attrs,
        )

        original = self._make_original(
            created_days_ago=42, last_seen_days_ago=5
        )
        before = original.last_seen_at

        # Build the same-fingerprint candidate the way views/jobs.py
        # would; resolve via find_duplicate; merge.
        candidate = self._make_candidate()
        candidate.canonical_link = None  # force fingerprint stage
        dupe = find_duplicate(candidate)
        self.assertEqual(dupe, original)
        merge_empty_fields_from_attrs(
            dupe,
            {"source": "extension", "description": "Fresh capture body"},
        )
        original.refresh_from_db()
        self.assertGreater(original.last_seen_at, before)


class TestJobPostExtractorBumpsLastSeen(TestCase):
    """Round-trip the extractor's persist tail: confirm last_seen_at
    advances on the scrape→existing-JP resolution path."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="ext-rolling", password="pass"
        )
        self.company = Company.objects.create(name="Allstate")

    def test_persist_tail_bumps_last_seen_on_existing_jp(self):
        """The extractor's ``_persist`` tail calls ``bump_last_seen``
        on every persist branch, so the post-scrape attach to an
        existing JP advances the rolling window without needing the
        full graph round-trip."""
        # Simulate the bump that the extractor performs at its
        # persist tail (``bump_last_seen(job)``). The aim of this
        # test is not to drive the full extractor — that surface has
        # heavy fixtures — but to lock the contract that every code
        # path that lands on an existing JP bumps the column. The
        # extractor test in tests/test_job_post_extractor*.py covers
        # the graph wiring; this checks the helper's invariant.
        now = timezone.now()
        jp = JobPost.objects.create(
            title="SWE",
            company=self.company,
            location="Remote",
            link="https://example.com/job/x",
            created_by=self.user,
        )
        JobPost.objects.filter(pk=jp.pk).update(
            last_seen_at=now - timedelta(days=10)
        )
        jp.refresh_from_db()
        old = jp.last_seen_at
        bump_last_seen(jp)
        jp.refresh_from_db()
        self.assertGreater(jp.last_seen_at, old)
