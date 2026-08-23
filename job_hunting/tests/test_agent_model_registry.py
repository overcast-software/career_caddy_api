"""BACK-132 — an AI role that is not in the registry is invisible.

``job_hunting.api.views.admin._agent_role_specs`` is the single list of AI
roles and their env overrides, surfaced by the staff-only
``GET /api/v1/agent-models/``. A role can read its own env var and still be
absent from that list, in which case nobody can see what it resolves to and it
quietly runs whatever its module-level default happens to be — forever.

That has now happened three times: ``answer`` and ``cover_letter`` (the two
roles whose prose a user sends an employer) and ``job_matcher``, which decides
which JobPost an application page belongs to and therefore which company's
context gets rendered into those answers. On 2026-08-12 it tied a Block
application to a Golden Analytics post at 0.9 confidence while running the
cheapest model available, and the admin page could not have told anyone.

Nothing covered ``_agent_role_specs`` at all before this file. These tests
exist so the registration cannot be silently dropped, and so the registry's
declared default and the matcher module's own ``_DEFAULT_MODEL`` cannot drift
apart — the registry claiming one model while the code runs another is the
same invisibility failure wearing a different hat.
"""
from __future__ import annotations

import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status as http_status
from rest_framework.test import APIClient

from job_hunting.api.views import admin as admin_views
from job_hunting.lib.parsers import job_matcher


User = get_user_model()


def _spec(role):
    """Return the registry row for ``role``, or None."""
    for entry in admin_views._agent_role_specs():
        if entry[0] == role:
            return entry
    return None


class JobMatcherRoleRegistrationTests(TestCase):
    """The registry entry itself — no HTTP, no auth."""

    def test_job_matcher_is_registered(self):
        entry = _spec("job_matcher")
        self.assertIsNotNone(
            entry,
            "job_matcher must appear in _agent_role_specs() or its model is "
            "invisible on the AI-roles admin surface and cannot be changed "
            "there.",
        )
        role, purpose, env_var, default = entry
        self.assertEqual(env_var, "JOB_MATCHER_MODEL")
        self.assertTrue(purpose.strip(), "the row needs a human-readable purpose")
        self.assertTrue(default, "the row needs a concrete default")

    def test_registry_default_matches_the_matcher_module_default(self):
        """The advertised default must be the one JobMatcher actually falls back to.

        These are two separate literals in two files on purpose (the view layer
        does not import the parser package). This test is what keeps them
        honest — bump one without the other and it fails here rather than
        misreporting on the admin page.
        """
        _, _, _, registry_default = _spec("job_matcher")
        self.assertEqual(registry_default, job_matcher._DEFAULT_MODEL)

    def test_job_matcher_is_off_the_cheap_global_default(self):
        """Deliberately asserts *inequality*, not a specific model id.

        Pinning the exact model would turn a future considered bump into a red
        test. What must not silently happen is the matcher sliding back onto
        the global cheap fallback — the state that produced the cross-company
        pick.
        """
        _, _, _, registry_default = _spec("job_matcher")
        self.assertNotEqual(registry_default, admin_views._DEFAULT_MODEL)
        self.assertNotEqual(job_matcher._DEFAULT_MODEL, admin_views._DEFAULT_MODEL)


class AgentModelsSurfaceTests(TestCase):
    """The staff-only endpoint that renders the registry."""

    URL = "/api/v1/agent-models/"

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="alice", password="pw")
        self.staff = User.objects.create_user(
            username="root", password="pw", is_staff=True
        )

    def _rows(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, http_status.HTTP_200_OK)
        return {row["role"]: row for row in resp.json()["data"]}

    def test_non_staff_forbidden(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, http_status.HTTP_403_FORBIDDEN)

    def test_staff_sees_job_matcher_row(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("JOB_MATCHER_MODEL", None)
            row = self._rows().get("job_matcher")

        self.assertIsNotNone(row, "job_matcher must be visible on the AI-roles page")
        self.assertEqual(row["env_var"], "JOB_MATCHER_MODEL")
        self.assertEqual(row["resolved"], job_matcher._DEFAULT_MODEL)
        self.assertEqual(row["source"], "default")

    def test_env_override_wins_on_the_surface(self):
        with mock.patch.dict(os.environ, {"JOB_MATCHER_MODEL": "openai:gpt-4o"}):
            row = self._rows()["job_matcher"]

        self.assertEqual(row["resolved"], "openai:gpt-4o")
        self.assertEqual(row["source"], "env")


class JobMatcherModelResolutionTests(TestCase):
    """Raising the default must not cost anyone the escape hatch."""

    def test_env_override_wins_over_the_default(self):
        with mock.patch.dict(os.environ, {"JOB_MATCHER_MODEL": "openai:gpt-4o-mini"}):
            self.assertEqual(job_matcher.JobMatcher().model_spec, "openai:gpt-4o-mini")

    def test_explicit_argument_wins_over_the_env(self):
        with mock.patch.dict(os.environ, {"JOB_MATCHER_MODEL": "openai:gpt-4o"}):
            matcher = job_matcher.JobMatcher(model="anthropic:claude-haiku-4-5")
        self.assertEqual(matcher.model_spec, "anthropic:claude-haiku-4-5")

    def test_falls_back_to_the_module_default(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("JOB_MATCHER_MODEL", None)
            self.assertEqual(
                job_matcher.JobMatcher().model_spec, job_matcher._DEFAULT_MODEL
            )
