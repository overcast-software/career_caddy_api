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

import secrets
from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from job_hunting.models import Invitation
from job_hunting.lib.username_policy import (
    RESERVED_USERNAMES,
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


class ReservedUsernameTests(TestCase):
    """CC-123 — a username may not be a site route, an API resource, or
    a service address. The list is one importable constant
    (`RESERVED_USERNAMES`) enforced inside `validate_username`, so every
    signup path that already calls the validator gets it for free."""

    def test_rejects_reserved_names(self):
        # One per derivation group, all charset-legal so the reserved
        # rule (not the charset rule) is what rejects them:
        #   frontend routes / API resources: login, setup, docs, users,
        #     companies, scores, profile, caddy, settings, admin
        #   same-origin root paths: api, mcp, events, actors
        #   service mailboxes: forwarding, noreply, postmaster, support
        #   infra hostnames: www, wiki, mail
        for bad in (
            "login", "setup", "docs", "users", "companies", "scores",
            "profile", "caddy", "settings", "admin", "api", "mcp",
            "events", "actors", "forwarding", "noreply", "postmaster",
            "support", "www", "wiki", "mail",
        ):
            self.assertFalse(
                is_valid_username(bad), f"expected {bad!r} to be reserved"
            )
            with self.assertRaises(UsernamePolicyError):
                validate_username(bad)

    def test_reserved_error_message_says_reserved(self):
        with self.assertRaises(UsernamePolicyError) as ctx:
            validate_username("forwarding")
        msg = str(ctx.exception).lower()
        self.assertIn("reserved", msg)
        self.assertIn("forwarding", msg)

    def test_ordinary_usernames_still_pass(self):
        # The list must not swallow normal names — including ones that
        # merely contain a reserved word as a substring.
        for ok in ("dough", "foobar", "api_dude", "admins", "mailman"):
            self.assertTrue(is_valid_username(ok), f"expected {ok!r} to pass")
            self.assertEqual(validate_username(ok), ok)

    def test_rejects_internal_account_names(self):
        # CC-123 review — `guest` is the highest-consequence name in the
        # list: `_guest_user_id()` (api/views/reports.py) resolves it by
        # literal, and `application_flow_report` is AllowAny, so whoever
        # owns the row has their pipeline served to anonymous callers.
        # `instance` is the server-level ActivityPub actor handle
        # (bootstrap_instance_actor.INSTANCE_USERNAME).
        for bad in ("guest", "instance"):
            self.assertFalse(
                is_valid_username(bad), f"expected {bad!r} to be reserved"
            )
            with self.assertRaises(UsernamePolicyError):
                validate_username(bad)

    def test_allow_reserved_bypasses_only_the_reserved_rule(self):
        # The escape hatch — used only by the read-only audit command —
        # lets a reserved name through...
        self.assertEqual(
            validate_username("admin", allow_reserved=True), "admin"
        )
        self.assertTrue(is_valid_username("api", allow_reserved=True))
        # ...but does NOT weaken charset or length.
        with self.assertRaises(UsernamePolicyError):
            validate_username("Admin", allow_reserved=True)
        with self.assertRaises(UsernamePolicyError):
            validate_username("ab", allow_reserved=True)

    def test_charset_error_wins_over_reserved(self):
        # `Login` is both malformed and reserved; the more specific
        # charset reason is the one the user is told.
        with self.assertRaises(UsernamePolicyError) as ctx:
            validate_username("Login")
        self.assertIn("lowercase", str(ctx.exception).lower())

    def test_list_covers_every_frontend_top_level_route(self):
        # Derived from frontend/app/router.js — the public profile page
        # is `this.route('profile', { path: '/:username' })`, so every
        # static top-level route beside it is a collision.
        for route in (
            "login", "logout", "setup", "waitlist", "signup", "about",
            "docs", "extension", "questions", "answers", "admin",
            "reports", "settings", "favorites", "caddy", "profile",
            "wizard", "users", "resumes", "scores", "scrapes",
            "companies", "summaries",
        ):
            self.assertIn(route, RESERVED_USERNAMES, f"route {route!r}")

    def test_list_covers_hyphenated_routes_and_resources(self):
        # Unreachable under the current `[a-z0-9_]` charset, but kept so
        # the list stays complete if the charset is ever retuned.
        for name in (
            "job-posts", "job-applications", "cover-letters",
            "career-data", "api-keys", "scrape-profiles",
            "accept-invite", "get-started",
        ):
            self.assertIn(name, RESERVED_USERNAMES, f"name {name!r}")

    def test_every_registered_api_resource_is_reserved(self):
        # The two assertions above compare one literal against another
        # literal in this same module, so they only bite on a deletion —
        # they cannot notice a *new* collision. This one derives the
        # left-hand side from the live DRF router, so the day someone
        # registers a viewset whose prefix is a legal username and
        # forgets `_API_RESOURCES`, this fails.
        #
        # The frontend half has no equivalent: router.js lives in a
        # different repo, so `_FRONTEND_TOP_LEVEL_ROUTES` stays a
        # hand-maintained literal by necessity.
        from job_hunting.urls import router

        registered = {prefix for prefix, _viewset, _basename in router.registry}
        self.assertTrue(registered, "router.registry unexpectedly empty")
        missing = sorted(registered - RESERVED_USERNAMES)
        self.assertEqual(
            missing, [],
            "API resources registered on the router but absent from "
            f"RESERVED_USERNAMES: {missing}",
        )


@override_settings(REGISTRATION_OPEN=True)
class ReservedUsernameOnSignupAPITests(TestCase):
    """The reserved list is enforced on the real signup surface —
    `DjangoUserViewSet.create`, which backs both `POST /api/v1/users/`
    and `POST /api/v1/auth/register/`."""

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
                        "email": f"{username}@example.com",
                        "password": "Abcd1234!Abcd",
                    },
                }
            },
            format="json",
        )

    def test_reserved_username_rejected_with_clear_error(self):
        resp = self._post_create("api")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("reserved", resp.json()["errors"][0]["detail"].lower())
        self.assertFalse(User.objects.filter(username="api").exists())

    def test_catchall_sink_username_rejected(self):
        # `forwarding` is the live catchall sink local-part; a user with
        # this name would have their mail discarded by the ingest filter.
        resp = self._post_create("forwarding")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(User.objects.filter(username="forwarding").exists())

    def test_register_endpoint_also_rejects(self):
        resp = self.client.post(
            "/api/v1/auth/register/",
            {
                "data": {
                    "type": "user",
                    "attributes": {
                        "username": "admin",
                        "email": "a@example.com",
                        "password": "Abcd1234!Abcd",
                    },
                }
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("reserved", resp.json()["errors"][0]["detail"].lower())

    def test_guest_username_rejected(self):
        # The highest-consequence name on the list. `_guest_user_id()`
        # (api/views/reports.py) resolves `username="guest"` by literal
        # and the AllowAny `application_flow_report` serves that user's
        # job-post + application pipeline to unauthenticated callers, so
        # on an instance where the demo account has not been seeded the
        # first registrant of `guest` gets their real pipeline published.
        resp = self._post_create("guest")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("reserved", resp.json()["errors"][0]["detail"].lower())
        self.assertFalse(User.objects.filter(username="guest").exists())

    def test_non_reserved_username_still_creates(self):
        resp = self._post_create("dough")
        self.assertEqual(resp.status_code, 201, resp.content)


class ReservedUsernameOnAcceptInviteTests(TestCase):
    """`POST /api/v1/accept-invite/` reaches the same validator through
    `_create_user_from_data` (api/views/_helpers.py). It is the one
    create path that works with `REGISTRATION_OPEN=False`, so it is
    where a reserved name would be taken on a closed instance — hence
    its own case rather than trust-by-construction."""

    def setUp(self):
        self.client = APIClient()
        self.token = secrets.token_urlsafe(32)
        self.invitation = Invitation.objects.create(
            email="invitee@example.com",
            token=self.token,
            expires_at=timezone.now() + timedelta(days=7),
        )

    def _accept(self, username):
        return self.client.post(
            "/api/v1/accept-invite/",
            {
                "token": self.token,
                "username": username,
                "password": "Abcd1234!Abcd",
            },
            format="json",
        )

    @override_settings(REGISTRATION_OPEN=False)
    def test_reserved_username_rejected_even_with_registration_closed(self):
        resp = self._accept("guest")
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("reserved", resp.json()["errors"][0]["detail"].lower())
        self.assertFalse(User.objects.filter(username="guest").exists())
        # The invitation is not burned by a rejected attempt.
        self.invitation.refresh_from_db()
        self.assertIsNone(self.invitation.accepted_at)

    @override_settings(REGISTRATION_OPEN=False)
    def test_non_reserved_username_still_accepts(self):
        resp = self._accept("invitee")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(User.objects.filter(username="invitee").exists())


class ReservedUsernameOnInitializeTests(TestCase):
    """`POST /api/v1/initialize/` enforces the reserved list with no
    escape hatch — including on its own zero-argument default, which
    CC-123 moved off the reserved name `admin` onto `owner` rather than
    grandfather the defect onto every no-config install. Initialize
    requires an empty user table, so each case runs against a fresh
    DB."""

    def setUp(self):
        self.client = APIClient()

    def test_explicit_reserved_username_rejected(self):
        resp = self.client.post(
            "/api/v1/initialize/",
            {
                "username": "forwarding",
                "email": "founder@example.com",
                "password": "Abcd1234!Abcd",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("reserved", resp.json()["errors"][0]["detail"].lower())
        self.assertEqual(User.objects.count(), 0)

    def test_explicitly_requested_admin_rejected(self):
        resp = self.client.post(
            "/api/v1/initialize/",
            {"username": "admin", "password": "Abcd1234!Abcd"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(User.objects.count(), 0)

    def test_zero_arg_bootstrap_defaults_to_a_non_reserved_name(self):
        # The no-config first run still works — it just no longer hands
        # the operator a reserved handle. Previously this created
        # `admin`, which the same PR reserves for the /admin route + the
        # RFC 2142 role mailbox.
        resp = self.client.post(
            "/api/v1/initialize/",
            {"password": "Abcd1234!Abcd"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(User.objects.get(username="owner").is_superuser)
        self.assertFalse(User.objects.filter(username="admin").exists())
        self.assertTrue(is_valid_username("owner"))

    def test_blank_username_still_falls_back_to_default(self):
        # An empty string is "not supplied", not a requested name.
        resp = self.client.post(
            "/api/v1/initialize/",
            {"username": "", "password": "Abcd1234!Abcd"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(User.objects.get(username="owner").is_superuser)

    def test_name_field_feeds_username_and_is_policed(self):
        # `name` has always doubled as the username source on this
        # endpoint (auth.py: `attrs.get("username") or attrs.get("name")`)
        # while also feeding first_name. Pinning the overload here so the
        # 400 below is a recorded decision rather than a surprise: a
        # `name` that lands on the username IS the username, so it clears
        # the same policy.
        resp = self.client.post(
            "/api/v1/initialize/",
            {"name": "caddy", "password": "Abcd1234!Abcd"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("reserved", resp.json()["errors"][0]["detail"].lower())
        self.assertEqual(User.objects.count(), 0)

    def test_non_reserved_name_field_still_initializes(self):
        resp = self.client.post(
            "/api/v1/initialize/",
            {"name": "founder", "password": "Abcd1234!Abcd"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        user = User.objects.get(username="founder")
        self.assertTrue(user.is_superuser)
        self.assertEqual(user.first_name, "founder")


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

    def test_default_username_still_initializes(self):
        # Omitting username falls back to the built-in default, which
        # must clear the policy — the no-argument bootstrap path keeps
        # working. CC-123 moved that default from "admin" (reserved) to
        # "owner"; what this case pins is that the zero-arg path still
        # produces a superuser whose name passes the validator.
        resp = self.client.post(
            "/api/v1/initialize/",
            {"password": "Abcd1234!Abcd"},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        created = User.objects.get()
        self.assertTrue(created.is_superuser)
        self.assertTrue(
            is_valid_username(created.username),
            f"default bootstrap username {created.username!r} fails the policy",
        )


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
        # A RESERVED but charset/length-clean row. Installs bootstrapped
        # before CC-123 moved the initialize default off `admin` all have
        # one of these; the audit's `allow_reserved=True` exists so they
        # are not reported forever. This row is what makes that flag
        # load-bearing — see test_reserved_username_not_flagged.
        self.reserved = User._default_manager.create(
            username="admin", email="ad@example.com"
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

    def test_reserved_username_not_flagged(self):
        # The audit's contract is charset + length only (CC-56 #58/#59).
        # `admin` is reserved for registration (CC-123) but is a
        # legitimate pre-existing superuser name on every install
        # bootstrapped before the default moved — reporting it would be a
        # permanent false violation. Drop `allow_reserved=True` from
        # audit_usernames.py and this fails.
        buf = StringIO()
        call_command("audit_usernames", stdout=buf)
        out = buf.getvalue()
        self.assertNotIn(f"{self.reserved.id}\tadmin", out)
        self.assertNotIn("reserved", out.lower())

    def test_does_not_mutate(self):
        before_username = self.legacy_upper.username
        call_command("audit_usernames", stdout=StringIO())
        self.legacy_upper.refresh_from_db()
        self.assertEqual(self.legacy_upper.username, before_username)

    def test_all_clean_prints_ok(self):
        # Delete every charset/length violator. `self.reserved` (admin)
        # deliberately stays, so "OK" here also proves a reserved-but-
        # well-formed name is not a violation for this command.
        self.legacy_upper.delete()
        self.legacy_plus.delete()
        self.legacy_dot.delete()
        self.legacy_short.delete()
        self.assertTrue(User.objects.filter(username="admin").exists())
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
