"""Unit tests for job_hunting.lib.url_policy."""

from unittest import mock

from django.test import SimpleTestCase

from job_hunting.lib.url_policy import (
    UrlPolicyError,
    validate_submission_url,
)


class TestValidateSubmissionUrl(SimpleTestCase):
    def test_https_public_host_passes(self):
        url = "https://jobs.lever.co/acme/123"
        self.assertEqual(validate_submission_url(url), url)

    def test_http_public_host_passes(self):
        self.assertEqual(
            validate_submission_url("http://example.com/jobs/1"),
            "http://example.com/jobs/1",
        )

    def test_strips_whitespace_before_parse(self):
        # raw is returned unchanged, but parsing must tolerate surrounding ws
        self.assertEqual(
            validate_submission_url("  https://example.com/x  "),
            "  https://example.com/x  ",
        )

    def test_blocks_self_host(self):
        with self.assertRaises(UrlPolicyError) as ctx:
            validate_submission_url("https://careercaddy.online/dashboard")
        self.assertEqual(ctx.exception.code, "blocked_self")

    def test_blocks_self_www_variant(self):
        with self.assertRaises(UrlPolicyError) as ctx:
            validate_submission_url("https://www.careercaddy.online/")
        self.assertEqual(ctx.exception.code, "blocked_self")

    def test_self_host_is_case_insensitive(self):
        with self.assertRaises(UrlPolicyError) as ctx:
            validate_submission_url("https://CareerCaddy.Online/x")
        self.assertEqual(ctx.exception.code, "blocked_self")

    def test_blocks_javascript_scheme(self):
        with self.assertRaises(UrlPolicyError) as ctx:
            validate_submission_url("javascript:alert(1)")
        self.assertEqual(ctx.exception.code, "blocked_scheme")

    def test_blocks_data_scheme(self):
        with self.assertRaises(UrlPolicyError) as ctx:
            validate_submission_url("data:text/html,<h1>x</h1>")
        self.assertEqual(ctx.exception.code, "blocked_scheme")

    def test_blocks_file_scheme(self):
        with self.assertRaises(UrlPolicyError) as ctx:
            validate_submission_url("file:///etc/passwd")
        self.assertEqual(ctx.exception.code, "blocked_scheme")

    def test_blocks_ftp_scheme(self):
        with self.assertRaises(UrlPolicyError) as ctx:
            validate_submission_url("ftp://example.com/x")
        self.assertEqual(ctx.exception.code, "blocked_scheme")

    def test_blocks_localhost_name(self):
        with self.assertRaises(UrlPolicyError) as ctx:
            validate_submission_url("http://localhost:4200/x")
        self.assertEqual(ctx.exception.code, "blocked_private")

    def test_blocks_localhost_ip(self):
        with self.assertRaises(UrlPolicyError) as ctx:
            validate_submission_url("http://127.0.0.1:8000/")
        self.assertEqual(ctx.exception.code, "blocked_private")

    def test_blocks_rfc1918_10(self):
        with self.assertRaises(UrlPolicyError) as ctx:
            validate_submission_url("http://10.0.0.5/x")
        self.assertEqual(ctx.exception.code, "blocked_private")

    def test_blocks_rfc1918_192(self):
        with self.assertRaises(UrlPolicyError) as ctx:
            validate_submission_url("http://192.168.1.42/x")
        self.assertEqual(ctx.exception.code, "blocked_private")

    def test_blocks_rfc1918_172(self):
        with self.assertRaises(UrlPolicyError) as ctx:
            validate_submission_url("http://172.16.0.1/x")
        self.assertEqual(ctx.exception.code, "blocked_private")

    def test_blocks_link_local(self):
        with self.assertRaises(UrlPolicyError) as ctx:
            validate_submission_url("http://169.254.169.254/")
        self.assertEqual(ctx.exception.code, "blocked_private")

    def test_blocks_dot_local_suffix(self):
        with self.assertRaises(UrlPolicyError) as ctx:
            validate_submission_url("http://printer.local/")
        self.assertEqual(ctx.exception.code, "blocked_private")

    def test_blocks_dot_internal_suffix(self):
        with self.assertRaises(UrlPolicyError) as ctx:
            validate_submission_url("http://api.internal/x")
        self.assertEqual(ctx.exception.code, "blocked_private")

    def test_blocks_empty_string(self):
        with self.assertRaises(UrlPolicyError) as ctx:
            validate_submission_url("")
        self.assertEqual(ctx.exception.code, "blocked_malformed")

    def test_blocks_none(self):
        with self.assertRaises(UrlPolicyError) as ctx:
            validate_submission_url(None)  # type: ignore[arg-type]
        self.assertEqual(ctx.exception.code, "blocked_malformed")

    def test_blocks_no_host(self):
        with self.assertRaises(UrlPolicyError) as ctx:
            validate_submission_url("https://")
        self.assertEqual(ctx.exception.code, "blocked_malformed")

    def test_extra_blocked_hosts_via_env(self):
        with mock.patch.dict(
            "os.environ", {"INGEST_BLOCKED_HOSTS": "evil.example,bad.test"}
        ):
            with self.assertRaises(UrlPolicyError) as ctx:
                validate_submission_url("https://evil.example/jobs/1")
            self.assertEqual(ctx.exception.code, "blocked_self")

    def test_public_ip_passes(self):
        # 8.8.8.8 is public — should not be flagged. Edge case; we expect
        # most legitimate ingest to use hostnames, but raw IPs aren't a
        # policy violation in Phase 0 unless they're private.
        self.assertEqual(
            validate_submission_url("https://8.8.8.8/x"),
            "https://8.8.8.8/x",
        )

    # --- mailto: opt-in apply target (recruiter direct solicitations) ---

    def test_mailto_blocked_by_default(self):
        # Scrape ingestion (the default caller) must keep rejecting mailto —
        # an email address is not scrapeable.
        with self.assertRaises(UrlPolicyError) as ctx:
            validate_submission_url("mailto:recruiter@acme.com")
        self.assertEqual(ctx.exception.code, "blocked_scheme")

    def test_mailto_allowed_when_opted_in(self):
        url = "mailto:recruiter@acme.com"
        self.assertEqual(validate_submission_url(url, allow_mailto=True), url)

    def test_mailto_returns_raw_unchanged(self):
        # Whitespace + query params survive verbatim (parsing tolerates them).
        url = "  mailto:hr@acme.io?subject=Apply  "
        self.assertEqual(validate_submission_url(url, allow_mailto=True), url)

    def test_mailto_empty_address_malformed(self):
        with self.assertRaises(UrlPolicyError) as ctx:
            validate_submission_url("mailto:", allow_mailto=True)
        self.assertEqual(ctx.exception.code, "blocked_malformed")

    def test_mailto_non_address_malformed(self):
        with self.assertRaises(UrlPolicyError) as ctx:
            validate_submission_url("mailto:notanemail", allow_mailto=True)
        self.assertEqual(ctx.exception.code, "blocked_malformed")

    def test_other_schemes_still_blocked_even_with_mailto_opt_in(self):
        # allow_mailto only relaxes mailto — javascript: stays blocked.
        with self.assertRaises(UrlPolicyError) as ctx:
            validate_submission_url("javascript:alert(1)", allow_mailto=True)
        self.assertEqual(ctx.exception.code, "blocked_scheme")
