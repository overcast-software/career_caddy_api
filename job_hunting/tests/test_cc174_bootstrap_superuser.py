"""CC-174 — bootstrap keys on superuser existence, not "any user".

A seeded/demo account (``make demo-data`` creates the Danny Noonan guest)
used to flip ``bootstrap_open`` to false and make ``POST
/api/v1/initialize/`` return 409, locking a fresh self-hoster out of
creating their own admin through the UI.

Secondary state the same ticket names: an un-migrated DB must report
``no_schema`` rather than bootstrap-open, so the setup wizard cannot
render (and be submitted) before ``migrate`` finishes.
"""
from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import DatabaseError, ProgrammingError
from django.test import TestCase
from rest_framework.test import APIClient

User = get_user_model()

# The single DB touch both endpoints share — patched to simulate the
# un-migrated DB without dropping a table out from under the test runner.
QUERYSET_EXISTS = "django.db.models.query.QuerySet.exists"


class BootstrapKeysOnSuperuserTests(TestCase):
    """Non-superuser rows must not close the bootstrap gate."""

    def setUp(self):
        self.client = APIClient()

    def _healthcheck(self):
        return self.client.get("/api/v1/healthcheck/").json()

    def _initialize_status(self):
        return self.client.get("/api/v1/initialize/").json()

    def test_empty_db_is_bootstrap_open(self):
        self.assertTrue(self._healthcheck()["bootstrap_open"])
        self.assertEqual(self._healthcheck()["bootstrap_state"], "bootstrap_open")
        self.assertTrue(self._initialize_status()["initialization_needed"])
        self.assertEqual(self._initialize_status()["status"], "needs_initialization")

    def test_demo_user_leaves_bootstrap_open(self):
        # Exactly the `make demo-data` shape: a real, non-admin account.
        User.objects.create_user(username="guest", password="p")
        self.assertTrue(self._healthcheck()["bootstrap_open"])
        self.assertEqual(self._healthcheck()["bootstrap_state"], "bootstrap_open")
        self.assertTrue(self._initialize_status()["initialization_needed"])
        self.assertEqual(self._initialize_status()["status"], "needs_initialization")

    def test_staff_but_not_superuser_leaves_bootstrap_open(self):
        User.objects.create_user(username="helper", password="p", is_staff=True)
        self.assertTrue(self._healthcheck()["bootstrap_open"])
        self.assertEqual(self._initialize_status()["status"], "needs_initialization")

    def test_superuser_closes_bootstrap(self):
        User.objects.create_superuser(username="founder", password="p")
        self.assertFalse(self._healthcheck()["bootstrap_open"])
        self.assertEqual(self._healthcheck()["bootstrap_state"], "initialized")
        self.assertFalse(self._initialize_status()["initialization_needed"])
        self.assertEqual(self._initialize_status()["status"], "initialized")

    def test_initialize_post_succeeds_alongside_demo_user(self):
        """The ticket's repro, end to end: demo data first, admin after."""
        User.objects.create_user(username="guest", password="p")
        resp = self.client.post(
            "/api/v1/initialize/",
            {
                "username": "founder",
                "email": "founder@example.com",
                "password": "Abcd1234!Abcd",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(User.objects.get(username="founder").is_superuser)
        # And the gate closes behind it.
        self.assertFalse(self._healthcheck()["bootstrap_open"])

    def test_initialize_post_refused_once_a_superuser_exists(self):
        User.objects.create_superuser(username="founder", password="p")
        resp = self.client.post(
            "/api/v1/initialize/",
            {
                "username": "second",
                "email": "second@example.com",
                "password": "Abcd1234!Abcd",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertEqual(resp.json()["bootstrap_state"], "initialized")
        self.assertFalse(User.objects.filter(username="second").exists())


class UnmigratedDatabaseTests(TestCase):
    """An un-migrated DB is its own state — never bootstrap-open."""

    def setUp(self):
        self.client = APIClient()

    def test_healthcheck_reports_no_schema_and_closes_bootstrap(self):
        with patch(
            QUERYSET_EXISTS,
            side_effect=ProgrammingError('relation "auth_user" does not exist'),
        ):
            resp = self.client.get("/api/v1/healthcheck/")
        body = resp.json()
        self.assertEqual(body["bootstrap_state"], "no_schema")
        self.assertFalse(body["bootstrap_open"])

    def test_healthcheck_is_unhealthy_when_user_table_unqueryable(self):
        """This endpoint is the api service's Cloud Run liveness probe
        (``health_path`` in deploy/terraform/gcp/locals.tf). A DB that is
        un-migrated — or simply down — must fail the probe, not read as a
        healthy instance that happens to be awaiting setup."""
        with patch(QUERYSET_EXISTS, side_effect=DatabaseError("connection refused")):
            resp = self.client.get("/api/v1/healthcheck/")
        self.assertEqual(resp.status_code, 503, resp.content)
        body = resp.json()
        self.assertFalse(body["healthy"])
        self.assertFalse(body["bootstrap_open"])
        # The state string survives the failure so ops can tell why.
        self.assertEqual(body["bootstrap_state"], "no_schema")

    def test_healthcheck_stays_healthy_when_only_bootstrap_is_open(self):
        resp = self.client.get("/api/v1/healthcheck/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["healthy"])
        self.assertEqual(resp.json()["bootstrap_state"], "bootstrap_open")

    def test_initialize_get_reports_no_schema(self):
        with patch(
            QUERYSET_EXISTS,
            side_effect=ProgrammingError('relation "auth_user" does not exist'),
        ):
            body = self.client.get("/api/v1/initialize/").json()
        self.assertEqual(body["status"], "no_schema")
        self.assertEqual(body["bootstrap_state"], "no_schema")
        self.assertFalse(body["initialization_needed"])

    def test_initialize_post_returns_503_not_a_failed_insert(self):
        with patch(QUERYSET_EXISTS, side_effect=DatabaseError("connection lost")):
            resp = self.client.post(
                "/api/v1/initialize/",
                {
                    "username": "founder",
                    "email": "founder@example.com",
                    "password": "Abcd1234!Abcd",
                },
                format="json",
            )
        self.assertEqual(resp.status_code, 503, resp.content)
        self.assertEqual(resp.json()["bootstrap_state"], "no_schema")
        self.assertEqual(User.objects.count(), 0)
