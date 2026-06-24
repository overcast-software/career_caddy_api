"""Username policy — catchall local-part + ActivityPub actor handle.

One rule (lives in `lib/username_policy.py`), enforced on every
user-create surface:
- API validator on every signup write path: `DjangoUserViewSet.create`,
  the `_create_user_from_data` shared helper (registration + invitation
  acceptance), AND `POST /api/v1/initialize/` (first-superuser create).
- `audit_usernames` management command — read-only audit of pre-
  existing rows that violate the rule (CC-56 #58 length + #59 charset).

The policy was tightened for CC-56 (the username is now a public actor
handle): minimum length 3 (#58) and charset `[a-z0-9_]` (#59) — dot and
hyphen are valid email local-part chars but invalid Mastodon handles, so
they are no longer accepted on new signups.

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

    def test_accepts_lowercase_alphanum_underscore(self):
        for ok in ("foo", "foo_bar", "f00", "abc", "x_y_z", "a1b", "dough"):
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

    def test_rejects_dot_and_hyphen(self):
        # CC-56 #59: dot and hyphen are valid email local-part chars but
        # invalid in a Mastodon/ActivityPub actor handle, so the charset
        # was tightened from `[a-z0-9._-]` to `[a-z0-9_]`. All of these
        # used to pass; they no longer do.
        for bad in ("foo.bar", "foo-bar", "a.b.c", "x-y-z", "foo..bar"):
            self.assertFalse(is_valid_username(bad))
            with self.assertRaises(UsernamePolicyError):
                validate_username(bad)

    def test_rejects_below_min_length(self):
        # CC-56 #58: floor is USERNAME_MIN_LENGTH (proposed 3).
        for bad in ("a", "ab", "x", "_"):
            self.assertFalse(is_valid_username(bad), f"expected {bad!r} to fail")
            with self.assertRaises(UsernamePolicyError):
                validate_username(bad)
        # Exactly at the floor passes.
        self.assertTrue(is_valid_username("abc"))

    def test_min_length_error_message_is_clear(self):
        with self.assertRaises(UsernamePolicyError) as ctx:
            validate_username("ab")
        self.assertIn("at least", str(ctx.exception).lower())

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
        resp = self._post_create("foobar")
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

    def test_dot_rejected(self):
        # Was valid under the catchall-only policy; rejected after #59.
        resp = self._post_create("foo.bar")
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_hyphen_rejected(self):
        resp = self._post_create("foo-bar")
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_short_username_rejected(self):
        resp = self._post_create("ab")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("at least", resp.json()["errors"][0]["detail"].lower())


class InitializeUsernamePolicyTests(TestCase):
    """`POST /api/v1/initialize/` creates the first superuser — whose
    username is the operator's public actor handle. It must enforce the
    same policy (CC-56 #58/#59). Initialize is only permitted on an empty
    user table, so each case runs against a fresh DB."""

    def setUp(self):
        self.client = APIClient()

    def _initialize(self, username):
        return self.client.post(
            "/api/v1/initialize/",
            {
                "username": username,
                "email": "founder@example.com",
                "password": "Abcd1234!Abcd",
            },
            format="json",
        )

    def test_valid_username_initializes(self):
        resp = self._initialize("founder")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(User.objects.count(), 1)
        self.assertTrue(User.objects.get(username="founder").is_superuser)

    def test_short_username_rejected(self):
        resp = self._initialize("ab")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("at least", resp.json()["errors"][0]["detail"].lower())
        # No user created on rejection — the table stays empty so the
        # operator can retry initialization.
        self.assertEqual(User.objects.count(), 0)

    def test_invalid_charset_rejected(self):
        for bad in ("foo.bar", "Founder", "foo bar"):
            resp = self._initialize(bad)
            self.assertEqual(resp.status_code, 400, resp.content)
            self.assertEqual(User.objects.count(), 0)

    def test_default_admin_username_still_initializes(self):
        # Omitting username falls back to "admin", which clears the
        # policy — the no-argument bootstrap path must keep working.
        resp = self.client.post(
            "/api/v1/initialize/",
            {"password": "Abcd1234!Abcd"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(User.objects.get(username="admin").is_superuser)


class AuditUsernamesCommandTests(TestCase):
    """`python manage.py audit_usernames` is read-only — prints
    violators with id/username/email/reason, never mutates. After the
    CC-56 tightening it catches both charset (#59) and length (#58)
    violators."""

    def setUp(self):
        # Bypass the validator by going straight to the ORM — these rows
        # simulate users that pre-date (or pre-date the tightening of)
        # the policy.
        self.legacy_upper = User._default_manager.create(
            username="LegacyMixed", email="lm@example.com"
        )
        self.legacy_plus = User._default_manager.create(
            username="legacy+plus", email="lp@example.com"
        )
        # Now-disallowed dot/hyphen (was clean under the catchall policy).
        self.legacy_dot = User._default_manager.create(
            username="old.handle", email="oh@example.com"
        )
        # Below the new min-length floor.
        self.legacy_short = User._default_manager.create(
            username="ab", email="ab@example.com"
        )
        # A clean row to prove it's NOT printed.
        self.clean = User.objects.create_user(
            username="foobar", email="fb@example.com", password="p"
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

    def test_charset_violator_listed(self):
        # CC-56 #59 — the now-disallowed dot username is flagged.
        buf = StringIO()
        call_command("audit_usernames", stdout=buf)
        out = buf.getvalue()
        self.assertIn("old.handle", out)
        self.assertIn(str(self.legacy_dot.id), out)

    def test_length_violator_listed(self):
        # CC-56 #58 — the below-floor username is flagged, with the
        # length reason surfaced in the row.
        buf = StringIO()
        call_command("audit_usernames", stdout=buf)
        out = buf.getvalue()
        self.assertIn(f"{self.legacy_short.id}\tab", out)
        self.assertIn("at least", out.lower())

    def test_clean_username_not_listed(self):
        buf = StringIO()
        call_command("audit_usernames", stdout=buf)
        out = buf.getvalue()
        # Assert the clean row itself isn't present by checking the
        # clean user's id+username tab-pair isn't in the output.
        self.assertNotIn(f"{self.clean.id}\tfoobar", out)

    def test_does_not_mutate(self):
        before_username = self.legacy_upper.username
        call_command("audit_usernames", stdout=StringIO())
        self.legacy_upper.refresh_from_db()
        self.assertEqual(self.legacy_upper.username, before_username)

    def test_all_clean_prints_ok(self):
        # Delete every violator so the DB has only clean usernames.
        self.legacy_upper.delete()
        self.legacy_plus.delete()
        self.legacy_dot.delete()
        self.legacy_short.delete()
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
            username="cc_auto_staff", password="p", is_staff=True
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
        resp = self.client.get("/api/v1/users/?filter[username]=cc_auto_staff")
        self.assertEqual(resp.status_code, 200, resp.content)
        ids = [r["id"] for r in resp.json()["data"]]
        self.assertEqual(ids, [str(self.target.id)])
