"""Phase 2.5 username catchall policy.

Two surfaces, one rule (lives in `lib/username_policy.py`):
- API validator on every signup write path (DjangoUserViewSet.create,
  `_create_user_from_data` shared helper used by registration +
  invitation acceptance).
- `audit_usernames` management command — read-only audit of pre-
  existing rows that violate the rule.

Also covers the `GET /api/v1/users/?filter[username]=…` query that
cc_auto's To-address resolver depends on (staff-gated; non-staff get
the existing self-only response unchanged).
"""

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from job_hunting.lib.username_policy import (
    UsernamePolicyError,
    is_valid_username,
    validate_username,
)

User = get_user_model()


class UsernamePolicyValidatorTests(TestCase):
    """Lib-level unit tests — the contract every signup path enforces."""

    def test_accepts_lowercase_alphanum_with_punct(self):
        for ok in ("foo", "foo.bar", "foo_bar", "foo-bar", "f00", "a", "x.y.z"):
            self.assertTrue(is_valid_username(ok), f"expected {ok!r} to pass")
            self.assertEqual(validate_username(ok), ok)

    def test_rejects_uppercase(self):
        for bad in ("Foo", "Foo Bar", "FOO", "fooBar"):
            self.assertFalse(is_valid_username(bad))
            with self.assertRaises(UsernamePolicyError):
                validate_username(bad)

    def test_rejects_email_chars(self):
        # The whole point of the policy: bare username must be a safe
        # local-part. @ and + would make the catchall ambiguous.
        for bad in ("foo@bar", "foo+bar", "foo bar", "foo,bar"):
            self.assertFalse(is_valid_username(bad))
            with self.assertRaises(UsernamePolicyError):
                validate_username(bad)

    def test_rejects_edge_dot_or_hyphen(self):
        # RFC 5321 forbids edge dots in local-parts. We mirror the rule
        # for hyphens because most mail providers reject them too.
        for bad in (".foo", "foo.", "-foo", "foo-", ".foo.", "-foo-"):
            self.assertFalse(is_valid_username(bad))

    def test_rejects_consecutive_dots(self):
        for bad in ("foo..bar", "..foo", "foo..", "a..b"):
            self.assertFalse(is_valid_username(bad))

    def test_rejects_empty_and_non_str(self):
        self.assertFalse(is_valid_username(""))
        with self.assertRaises(UsernamePolicyError):
            validate_username("")
        with self.assertRaises(UsernamePolicyError):
            validate_username(None)  # type: ignore[arg-type]

    def test_rejects_overlong(self):
        # USERNAME_MAX_LENGTH=150 matches Django default.
        with self.assertRaises(UsernamePolicyError):
            validate_username("a" * 151)
        self.assertTrue(is_valid_username("a" * 150))

    def test_validator_does_not_coerce(self):
        # Silent lowercase coercion would let a typo slip through and
        # break the user's later login attempt. The validator returns
        # the input unchanged or raises — never alters.
        with self.assertRaises(UsernamePolicyError):
            validate_username("FooBar")


@override_settings(REGISTRATION_OPEN=True)
class UsernamePolicyOnSignupAPITests(TestCase):
    """API-level coverage — the validator is wired into both signup
    write paths (DjangoUserViewSet.create + _create_user_from_data
    helper used by registration + invitation acceptance)."""

    def setUp(self):
        self.client = APIClient()

    def _post_create(self, username):
        return self.client.post(
            "/api/v1/users/",
            {
                "data": {
                    "type": "user",
                    "attributes": {
                        "username": username,
                        "email": f"{username.replace('@', '_at_')}@example.com",
                        "password": "Abcd1234!Abcd",
                    },
                }
            },
            format="json",
        )

    def test_valid_username_creates_user(self):
        resp = self._post_create("foo.bar")
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_uppercase_rejected(self):
        resp = self._post_create("FooBar")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("lowercase", resp.json()["errors"][0]["detail"].lower())

    def test_plus_rejected(self):
        resp = self._post_create("foo+bar")
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_at_rejected(self):
        resp = self._post_create("foo@bar")
        self.assertEqual(resp.status_code, 400, resp.content)


class AuditUsernamesCommandTests(TestCase):
    """`python manage.py audit_usernames` is read-only — prints
    violators with id/username/email/reason, never mutates."""

    def setUp(self):
        # Bypass the validator by going straight to the ORM — these
        # rows simulate users that pre-date the Phase 2.5 policy.
        self.legacy_upper = User._default_manager.create(
            username="LegacyMixed", email="lm@example.com"
        )
        self.legacy_plus = User._default_manager.create(
            username="legacy+plus", email="lp@example.com"
        )
        # A clean row to prove it's NOT printed.
        self.clean = User.objects.create_user(
            username="foo.bar", email="fb@example.com", password="p"
        )

    def test_violators_printed_with_id_username_email(self):
        buf = StringIO()
        call_command("audit_usernames", stdout=buf)
        out = buf.getvalue()

        self.assertIn(str(self.legacy_upper.id), out)
        self.assertIn("LegacyMixed", out)
        self.assertIn("lm@example.com", out)

        self.assertIn(str(self.legacy_plus.id), out)
        self.assertIn("legacy+plus", out)
        self.assertIn("lp@example.com", out)

    def test_clean_username_not_listed(self):
        buf = StringIO()
        call_command("audit_usernames", stdout=buf)
        out = buf.getvalue()
        # `foo.bar` could legitimately appear if some line printed by
        # the header includes a literal — assert the row itself isn't
        # present by checking the clean user's id isn't in the output.
        self.assertNotIn(f"{self.clean.id}\tfoo.bar", out)

    def test_does_not_mutate(self):
        before_username = self.legacy_upper.username
        call_command("audit_usernames", stdout=StringIO())
        self.legacy_upper.refresh_from_db()
        self.assertEqual(self.legacy_upper.username, before_username)

    def test_all_clean_prints_ok(self):
        # Delete the legacy rows so the DB has only clean usernames.
        self.legacy_upper.delete()
        self.legacy_plus.delete()
        buf = StringIO()
        call_command("audit_usernames", stdout=buf)
        self.assertIn("OK", buf.getvalue())


class UsernameFilterEndpointTests(TestCase):
    """cc_auto's catchall resolver issues
    `GET /api/v1/users/?filter[username]=<local-part>` to map the
    To-address local-part to a user id. The endpoint must be staff-
    gated (the resolver runs under a staff API key) and return at
    most one row (username is unique)."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username="cc-auto-staff", password="p", is_staff=True
        )
        self.target = User.objects.create_user(
            username="dough", password="p", email="dough@example.com"
        )
        self.client = APIClient()

    def test_staff_filter_username_returns_match(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.get("/api/v1/users/?filter[username]=dough")
        self.assertEqual(resp.status_code, 200, resp.content)
        ids = [r["id"] for r in resp.json()["data"]]
        self.assertEqual(ids, [str(self.target.id)])

    def test_staff_filter_username_no_match_returns_empty(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.get("/api/v1/users/?filter[username]=nope")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"], [])

    def test_non_staff_filter_username_still_returns_only_self(self):
        # The filter is a staff convenience; non-staff still get the
        # existing self-only response (no change to the safety guarantee).
        self.client.force_authenticate(user=self.target)
        resp = self.client.get("/api/v1/users/?filter[username]=cc-auto-staff")
        self.assertEqual(resp.status_code, 200, resp.content)
        ids = [r["id"] for r in resp.json()["data"]]
        self.assertEqual(ids, [str(self.target.id)])
