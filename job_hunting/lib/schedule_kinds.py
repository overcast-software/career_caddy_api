"""Schedule registry — the single source mapping a recurring sweep NAME to its
worker fn (CC-213).

The django-q2 ``qcluster`` worker used to run these on ``django_q.Schedule``
rows (registered by data migrations 0086/0090/0109/0113). Cloud Tasks is a
queue, not a cron, and GCP has no ``Job`` runner — so on GCP the recurring
clock is **Cloud Scheduler**, which POSTs ``{"name": "<sweep>"}`` to the
generic ``/tasks/run-scheduled/`` handler on the tasks Cloud Run service. On
self-host the ``manage.py run_jobs`` loop runs the same registry on cadence.
One registry, two drivers — the same transport-split shape as
``job_kinds.KIND_REGISTRY`` / ``enqueue``.

Both drivers consult this ONE dict so they can never drift. Worker fns are the
EXISTING pure sweep functions (idempotent, read-mostly); this module only maps
name → fn + declares the cadence so the two drivers agree.

To add a sweep: add its ``ScheduleSpec`` here, add the matching
``google_cloud_scheduler_job`` in ``deploy/terraform/gcp/scheduler.tf`` with
the SAME name + cron, and the run_jobs loop picks it up automatically.

CONTRACT with scheduler.tf (must match exactly):
- handler path: ``/tasks/run-scheduled/``
- request body: ``{"name": "<sweep-name>"}`` (JSON)
- the ``<sweep-name>`` keys are exactly ``SCHEDULE_REGISTRY``'s keys below.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module


@dataclass(frozen=True)
class ScheduleSpec:
    """One recurring sweep: dotted worker path + its cadence.

    ``dotted`` is resolved lazily (see ``resolve_schedule``) so importing this
    module stays cheap and doesn't drag in the scrape/federation graphs at
    Django startup. ``interval_seconds`` is the self-host ``run_jobs`` cadence
    and documents the cron the Cloud Scheduler job in scheduler.tf must match.
    """

    dotted: str
    interval_seconds: int


# name -> ScheduleSpec. Cadences mirror the django-q2 Schedule rows the sweeps
# were registered with (migration in the comment). These four are the LIVE
# sweeps that have a real worker fn.
#
# NOTE: migration 0111 registered a fifth Schedule name
# ``sweep_orphaned_attended_holds`` whose worker fn was NEVER implemented in
# ``lib/tasks.py`` (nor the ``CC_ATTENDED_HOLD_*`` settings it referenced) — a
# dead registration that ImportErrors if the qcluster worker ever tries to run
# it. It is deliberately NOT in this registry (there is no fn to put behind
# it); pointing a Cloud Scheduler job at a non-existent fn would just relocate
# the ImportError. Flagged on CC-213 for a follow-up (implement the sweep, or
# drop the 0111 Schedule).
SCHEDULE_REGISTRY: dict[str, ScheduleSpec] = {
    # 0086 — reset crashed-runner scrape claims back to 'hold'.
    "sweep_stale_scrape_claims": ScheduleSpec(
        "job_hunting.lib.tasks.sweep_stale_scrape_claims", 300
    ),
    # 0090 — re-drive overdue ActivityPub outbound (federation retry engine).
    "federation_dispatch_sweep": ScheduleSpec(
        "job_hunting.lib.federation_dispatch.sweep_pending_dispatches", 60
    ),
    # 0109 — null html on all but the newest completed scrape per host.
    "prune_scrape_html": ScheduleSpec(
        "job_hunting.lib.tasks.prune_scrape_html", 3600
    ),
    # 0113 — scrape.holds.stale observability WARNING (live in prod).
    "sweep_stale_unclaimed_holds": ScheduleSpec(
        "job_hunting.lib.tasks.sweep_stale_unclaimed_holds", 300
    ),
}


class UnknownSchedule(KeyError):
    """Raised when a handler/runner references a sweep not in the registry."""


def resolve_schedule(name: str):
    """Return the callable sweep fn registered for ``name``.

    Raises ``UnknownSchedule`` if the name isn't registered — the caller
    decides whether that's a terminal 200 (handler: a bad name never becomes
    good) or a logged skip (runner). Never dispatches an arbitrary importable:
    only the fixed set of dotted paths in ``SCHEDULE_REGISTRY`` is reachable.
    """
    spec = SCHEDULE_REGISTRY.get(name)
    if spec is None:
        raise UnknownSchedule(name)
    module_path, _, attr = spec.dotted.rpartition(".")
    module = import_module(module_path)
    return getattr(module, attr)
