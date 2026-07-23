"""CC-213 — recurring-sweep transport: SCHEDULE_REGISTRY + /tasks/run-scheduled/
handler + the self-host run_jobs re-arm.

The 5 django-q2 ``Schedule`` cron rows the qcluster worker owned move onto:
- GCP: Cloud Scheduler → ``/tasks/run-scheduled/`` (dispatch by name via
  ``SCHEDULE_REGISTRY``), and
- self-host: the ``run_jobs`` loop running the same registry on cadence.

These tests drive the handler via the Django test client (TESTING skips the
Cloud Scheduler header guard) and the runner via ``call_command``. The sweep
fns are patched at the registry's dotted paths so the tests exercise the
DISPATCH mechanics, not the sweeps' own DB work (covered elsewhere).
"""
import json
from unittest.mock import patch

from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse

from job_hunting.lib.schedule_kinds import (
    SCHEDULE_REGISTRY,
    UnknownSchedule,
    resolve_schedule,
)


class TestScheduleRegistry(TestCase):
    def test_registry_holds_the_four_live_sweeps(self):
        self.assertEqual(
            set(SCHEDULE_REGISTRY),
            {
                "sweep_stale_scrape_claims",
                "federation_dispatch_sweep",
                "prune_scrape_html",
                "sweep_stale_unclaimed_holds",
            },
        )

    def test_cadences_mirror_the_django_q_schedules(self):
        # Seconds; mirror the migration intervals (0086/0090/0109/0113).
        self.assertEqual(
            SCHEDULE_REGISTRY["sweep_stale_scrape_claims"].interval_seconds, 300
        )
        self.assertEqual(
            SCHEDULE_REGISTRY["federation_dispatch_sweep"].interval_seconds, 60
        )
        self.assertEqual(
            SCHEDULE_REGISTRY["prune_scrape_html"].interval_seconds, 3600
        )
        self.assertEqual(
            SCHEDULE_REGISTRY["sweep_stale_unclaimed_holds"].interval_seconds, 300
        )

    def test_resolve_imports_the_worker_fn(self):
        fn = resolve_schedule("prune_scrape_html")
        self.assertTrue(callable(fn))
        self.assertEqual(fn.__name__, "prune_scrape_html")

    def test_resolve_unknown_raises(self):
        with self.assertRaises(UnknownSchedule):
            resolve_schedule("does_not_exist")

    def test_attended_hold_sweep_is_not_registered(self):
        # 0111's sweep_orphaned_attended_holds has no worker fn — a dead
        # registration that must NOT be behind the registry (would ImportError).
        self.assertNotIn("sweep_orphaned_attended_holds", SCHEDULE_REGISTRY)


class TestRunScheduledHandler(TestCase):
    def setUp(self):
        self.client = Client()
        self.url = reverse("tasks-run-scheduled")

    def _post(self, body):
        return self.client.post(
            self.url, data=json.dumps(body), content_type="application/json"
        )

    def test_dispatches_named_sweep(self):
        with patch(
            "job_hunting.lib.tasks.prune_scrape_html",
            return_value={"nulled": 3, "kept": 5},
        ) as mock_sweep:
            resp = self._post({"name": "prune_scrape_html"})
        self.assertEqual(resp.status_code, 200)
        mock_sweep.assert_called_once_with()
        body = resp.json()
        self.assertEqual(body["nulled"], 3)
        self.assertEqual(body["status"], "completed")

    def test_non_dict_sweep_result_is_wrapped(self):
        # sweep_pending_dispatches returns an int — must still be a JSON object.
        with patch(
            "job_hunting.lib.federation_dispatch.sweep_pending_dispatches",
            return_value=7,
        ):
            resp = self._post({"name": "federation_dispatch_sweep"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["result"], 7)
        self.assertEqual(body["status"], "completed")

    def test_idempotent_double_fire(self):
        # At-least-once safety: firing twice just runs the idempotent sweep
        # twice — both return 200.
        with patch(
            "job_hunting.lib.tasks.sweep_stale_unclaimed_holds",
            return_value={"hold_unclaimed_stale": 0},
        ) as mock_sweep:
            r1 = self._post({"name": "sweep_stale_unclaimed_holds"})
            r2 = self._post({"name": "sweep_stale_unclaimed_holds"})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(mock_sweep.call_count, 2)

    def test_unknown_name_is_terminal_200(self):
        resp = self._post({"name": "not_a_sweep"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "unknown_schedule")

    def test_missing_name_400(self):
        resp = self._post({})
        self.assertEqual(resp.status_code, 400)

    def test_invalid_json_400(self):
        resp = self.client.post(
            self.url, data="not json", content_type="application/json"
        )
        self.assertEqual(resp.status_code, 400)

    def test_rejects_get(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_sweep_exception_propagates_to_500(self):
        with patch(
            "job_hunting.lib.tasks.prune_scrape_html",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                self._post({"name": "prune_scrape_html"})

    def test_start_end_logged(self):
        logger_name = "job_hunting.api.views.tasks_handlers"
        with patch(
            "job_hunting.lib.tasks.prune_scrape_html", return_value={"nulled": 0}
        ):
            with self.assertLogs(logger_name, level="INFO") as cm:
                self._post({"name": "prune_scrape_html"})
        text = "\n".join(cm.output)
        self.assertIn("run-scheduled: START name=prune_scrape_html", text)
        self.assertIn("run-scheduled: END name=prune_scrape_html", text)
        self.assertIn("duration_ms=", text)


class TestRunJobsSelfHostRearm(TestCase):
    """--once runs every due recurring sweep once (the self-host driver)."""

    def test_once_runs_all_registered_sweeps(self):
        with patch(
            "job_hunting.lib.tasks.sweep_stale_scrape_claims",
            return_value={"reset": 0},
        ) as m_claims, patch(
            "job_hunting.lib.federation_dispatch.sweep_pending_dispatches",
            return_value=0,
        ) as m_fed, patch(
            "job_hunting.lib.tasks.prune_scrape_html", return_value={"nulled": 0}
        ) as m_prune, patch(
            "job_hunting.lib.tasks.sweep_stale_unclaimed_holds",
            return_value={"hold_unclaimed_stale": 0},
        ) as m_holds:
            call_command(
                "run_jobs", "--once", "--runner-name", "test", "--sweep-every", "0"
            )
        m_claims.assert_called_once_with()
        m_fed.assert_called_once_with()
        m_prune.assert_called_once_with()
        m_holds.assert_called_once_with()

    def test_a_failing_sweep_does_not_stop_the_others(self):
        with patch(
            "job_hunting.lib.tasks.sweep_stale_scrape_claims",
            side_effect=RuntimeError("boom"),
        ), patch(
            "job_hunting.lib.federation_dispatch.sweep_pending_dispatches",
            return_value=0,
        ) as m_fed, patch(
            "job_hunting.lib.tasks.prune_scrape_html", return_value={"nulled": 0}
        ) as m_prune, patch(
            "job_hunting.lib.tasks.sweep_stale_unclaimed_holds",
            return_value={"hold_unclaimed_stale": 0},
        ) as m_holds:
            call_command(
                "run_jobs", "--once", "--runner-name", "test", "--sweep-every", "0"
            )
        # The RuntimeError in the first sweep is isolated; the rest still ran.
        m_fed.assert_called_once_with()
        m_prune.assert_called_once_with()
        m_holds.assert_called_once_with()
