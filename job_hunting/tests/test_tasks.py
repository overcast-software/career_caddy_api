"""Phase 1 tests for the django-q2 task module.

Plans/Job-queue integration — django-q2 phased rollout, sub-phase 1.
The tests here cover three things:

1. The tasks module imports cleanly. Most of Phase 1's failure modes
   are import-time (wrong settings key, missing dep, INSTALLED_APPS
   ordering). A single import test guards against regressions.

2. The `health_check` task returns the expected payload shape.
   Subsequent phases will add real tasks; the contract this test pins
   (return value is JSON-serializable, side-effect-free, deterministic
   under a fixed `message` arg) carries forward.

3. django_q is wired into INSTALLED_APPS and Q_CLUSTER is configured.
   These are the only behavior-bearing settings changes in Phase 1;
   the rest of the migration phases assume they exist and override
   per-task knobs.

Out of scope for this test file: actually running a qcluster worker.
The end-to-end smoke test (enqueue → worker picks up → completion)
runs via `docker compose up worker` + a manual shell enqueue per the
plan's Phase 1 verification section.
"""
from __future__ import annotations

import time

from django.conf import settings
from django.test import TestCase


class TestHealthCheckTask(TestCase):
    def test_default_payload(self):
        from job_hunting.lib.tasks import health_check

        before = time.time()
        result = health_check()
        after = time.time()

        self.assertIsInstance(result, dict)
        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], "health_check ran")
        self.assertGreaterEqual(result["ts"], before)
        self.assertLessEqual(result["ts"], after)

    def test_custom_message_passes_through(self):
        from job_hunting.lib.tasks import health_check

        result = health_check("smoke-from-test")
        self.assertEqual(result["message"], "smoke-from-test")

    def test_module_import_does_not_trigger_side_effects(self):
        """Importing the tasks module must NOT touch the DB, network,
        or any LLM. The qcluster process imports this module at every
        worker boot; any side effect there magnifies the boot cost +
        becomes a release-blocking surprise."""
        import importlib

        import job_hunting.lib.tasks as mod

        # Re-import; if there are side effects they fire here. The
        # assertion is just that re-importing doesn't raise.
        importlib.reload(mod)
        self.assertTrue(hasattr(mod, "health_check"))


class TestDjangoQWiring(TestCase):
    def test_django_q_in_installed_apps(self):
        self.assertIn("django_q", settings.INSTALLED_APPS)

    def test_q_cluster_configured(self):
        self.assertTrue(hasattr(settings, "Q_CLUSTER"))
        q = settings.Q_CLUSTER
        # Sanity-check the load-bearing keys; the plan node has the
        # rationale for each default value.
        self.assertEqual(q["orm"], "default")
        self.assertEqual(q["name"], "career_caddy")
        self.assertGreaterEqual(q["workers"], 1)
        # Default timeout must be high enough for Phase 2's Score / Summary
        # tier-1 LLM calls but not so high that a hung task chews a worker
        # forever. 300s is the documented Phase 1 baseline.
        self.assertGreaterEqual(q["timeout"], 60)
        self.assertLessEqual(q["timeout"], 900)
        # No automatic retries — per-task overrides only.
        self.assertEqual(q.get("max_attempts", 1), 1)
