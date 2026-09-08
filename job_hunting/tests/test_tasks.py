"""Tests for the task module.

Plans/Job-queue integration — django-q2 phased rollout, sub-phase 1.
The tests here cover three things:

1. The tasks module imports cleanly. Most of Phase 1's failure modes
   are import-time (wrong settings key, missing dep, INSTALLED_APPS
   ordering). A single import test guards against regressions.

2. The `health_check` task returns the expected payload shape.
   Subsequent phases will add real tasks; the contract this test pins
   (return value is JSON-serializable, side-effect-free, deterministic
   under a fixed `message` arg) carries forward.

3. django-q2 is GONE (CC-208) and stays gone. This item used to assert
   the opposite — that django_q was in INSTALLED_APPS and Q_CLUSTER was
   configured. The qcluster worker was a temporary bridge that outlived
   its blocker by five weeks, so its absence is now pinned rather than
   left to drift back in.

Out of scope: running a worker. Async dispatch is the unified enqueue()
producer — Cloud Tasks on GCP, a Job row drained by `manage.py run_jobs`
on self-host, selected by CC_TASKS_ENABLED.
"""
from __future__ import annotations

import pathlib
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


class TestDjangoQRemoved(TestCase):
    """CC-208 — django-q2 is gone. This is the regression guard.

    It replaces ``TestDjangoQWiring``, which asserted the opposite. The
    qcluster bridge worker (CC-199) was always a STRICTLY TEMPORARY drainer
    for jobs stranded by the CC-169 push, and Doug was explicit that it must
    not become permanent. It outlived its blocker by five weeks. Asserting its
    ABSENCE is what stops it drifting back in.
    """

    def test_django_q_not_in_installed_apps(self):
        self.assertNotIn("django_q", settings.INSTALLED_APPS)

    def test_no_q_cluster_setting(self):
        self.assertFalse(hasattr(settings, "Q_CLUSTER"))

    def test_nothing_imports_django_q(self):
        """The package may still sit in a stale venv; nothing may USE it.

        Walks the source rather than shelling out to git — the container has
        no git binary, and a test that silently depends on one is a test that
        fails for a reason unrelated to what it checks.
        """
        import ast

        import job_hunting

        root = pathlib.Path(job_hunting.__file__).parent
        offenders = []
        for path in root.rglob("*.py"):
            if "/tests/" in str(path) or "/migrations/" in str(path):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                # ast, not a line scan: a line scan counts docstring EXAMPLES
                # as imports. tasks.py had two, and reporting them as live
                # code would make this guard cry wolf until someone deleted it.
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                    "django_q"
                ):
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")
                elif isinstance(node, ast.Import) and any(
                    a.name.startswith("django_q") for a in node.names
                ):
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")

        self.assertEqual(
            offenders,
            [],
            "live django_q imports remain:\n" + "\n".join(offenders),
        )

    def test_the_replacement_is_wired(self):
        """Every async path resolves through the one registry."""
        from job_hunting.lib.job_kinds import KIND_REGISTRY

        self.assertIn("cover_letter", KIND_REGISTRY)
        self.assertEqual(
            KIND_REGISTRY["cover_letter"],
            "job_hunting.lib.tasks.cover_letter_job",
        )
