"""Scrape lifecycle helpers.

Once contained the synchronous `Scraper` class that POSTed to a
browser-MCP HTTP endpoint and updated the Scrape row in a background
thread. That path is gone — every scrape now starts as `status=hold`
and the hold-poller (agents/pollers/hold_poller.py) drives extraction.
What remains here are the bookkeeping helpers that the views still
call to log status transitions and (post-extraction) parse content
into a JobPost.
"""
import logging

logger = logging.getLogger(__name__)


def _maybe_caddy_extract(scrape, force: bool = False) -> None:
    """Parse job_content and create JobPost + Company via JobPostExtractor.

    By default, skips when scrape.job_post_id is set (first-pass idempotency).
    Exception: if the linked post is flagged !complete, parse anyway with
    force=True so the common "click Run scrape on a stub to enrich it"
    flow actually updates the description. Posts flagged complete=True
    remain idempotent — a repeat scrape is a no-op unless the caller
    passes force=True explicitly.
    """
    from job_hunting.lib.parsers.job_post_extractor import parse_scrape
    if not scrape.job_content:
        return
    if scrape.job_post_id and not force:
        from job_hunting.models import JobPost
        linked = JobPost.objects.filter(pk=scrape.job_post_id).only("complete").first()
        if linked is None or linked.complete:
            return
        force = True
    parse_scrape(
        scrape.id,
        user_id=getattr(scrape.created_by, "id", None),
        sync=True,
        force=force,
    )


def _set_scrape_status(scrape_id: int, status: str) -> None:
    try:
        from job_hunting.models.scrape import Scrape
        Scrape.objects.filter(pk=scrape_id).update(status=status)
    except Exception:
        logger.exception("_set_scrape_status failed scrape_id=%s status=%s", scrape_id, status)


def _log_scrape_status(
    scrape_id: int,
    status_label: str,
    note: str = None,
    graph_node: str = None,
    graph_payload: dict = None,
    update_scrape_status: bool = True,
    failure_reason: str = None,
) -> None:
    """Append a ScrapeStatus audit record (and optionally bump Scrape.status).

    graph_node / graph_payload are populated by the scrape-graph
    runner's tracing mixin; legacy callers leave them None.

    update_scrape_status=False skips the =Scrape.status= write — used
    by graph-transition so per-node trace rows don't clobber the
    legacy terminal status ('completed' / 'failed') that the frontend
    polls on. Otherwise the shadow-mode pipeline lands on
    'resolveapplyurl' and UI spinners never terminate.

    failure_reason: human-readable summary of why this scrape didn't
    produce a JobPost. Only meaningful when status_label='failed';
    silently ignored otherwise. Persisted on the Scrape row (not the
    ScrapeStatus audit row) so the operator-facing surfaces — extension
    popup, scrapes.show, dedupe report — can read it via the
    ScrapeSerializer without sideloading the full scrape_statuses
    collection. Truncated to the model's max_length (2000) so a
    runaway traceback can't blow the column.
    """
    try:
        from job_hunting.models.scrape import Scrape
        from job_hunting.models.scrape_status import ScrapeStatus
        from job_hunting.models.status import Status
        from django.utils import timezone

        if update_scrape_status:
            # Heartbeat the claim — the lease sweep in Phase 2 will
            # reset rows whose claimed_at is older than 15 min, so
            # bumping it on each status update keeps a long-running
            # runner from getting reaped mid-scrape. On terminal status
            # we also clear claimed_at + claimed_by so post-mortem
            # queries can distinguish "claimed and finished" from
            # "still claimed by a live runner."
            update_fields = {"status": status_label}
            if status_label in ("completed", "failed"):
                update_fields["claimed_at"] = None
                update_fields["claimed_by"] = None
            elif status_label != "hold":
                # `hold` is the pre-claim queue state — no runner owns the
                # row yet, so heartbeating claimed_at here would cause
                # claim-next (filter: claimed_at IS NULL) to skip it
                # forever. Only bump for in-flight non-terminal states
                # (running, extracting, updating_profile, …) that an
                # active runner owns and needs to keep the lease on.
                update_fields["claimed_at"] = timezone.now()
            # Persist the operator-facing diagnostic on terminal
            # failures only — truncated to fit the column. Non-failed
            # status transitions silently ignore the kwarg so a stray
            # caller can't poison the column on a 'completed' write.
            if status_label == "failed" and failure_reason:
                update_fields["failure_reason"] = str(failure_reason)[:2000]
            Scrape.objects.filter(pk=scrape_id).update(**update_fields)
            # Emit a terminal-status notification when the Scrape lands
            # on completed/failed so frontend SSE subscribers can update
            # the row without polling. Non-terminal status writes (hold,
            # running, extracting, updating_profile, …) intentionally
            # don't fire — keeps the event stream low-noise. See
            # Plans/Push status updates — SSE replaces polling cap.
            if status_label in ("completed", "failed"):
                from job_hunting.lib import events
                scrape = Scrape.objects.filter(pk=scrape_id).only(
                    "created_by_id"
                ).first()
                user_id = scrape.created_by_id if scrape else None
                events.notify("scrape", scrape_id, status_label, user_id)
        status_obj, _ = Status.objects.get_or_create(
            status=status_label, defaults={"status_type": "scrape"}
        )
        ScrapeStatus.objects.create(
            scrape_id=scrape_id,
            status=status_obj,
            logged_at=timezone.now(),
            note=note,
            graph_node=graph_node,
            graph_payload=graph_payload,
        )

        # Mark domain as requiring auth on login_failed
        if status_label in ("login_failed",):
            try:
                from urllib.parse import urlparse
                from job_hunting.models import ScrapeProfile
                scrape = Scrape.objects.filter(pk=scrape_id).first()
                if scrape and scrape.url:
                    hostname = urlparse(scrape.url).hostname or ""
                    if hostname.startswith("www."):
                        hostname = hostname[4:]
                    if hostname:
                        ScrapeProfile.objects.update_or_create(
                            hostname=hostname,
                            defaults={"requires_auth": True},
                        )
                        logger.info("Marked %s as requires_auth", hostname)
            except Exception:
                logger.debug("Failed to mark requires_auth", exc_info=True)

    except Exception:
        logger.exception(
            "_log_scrape_status failed scrape_id=%s status=%s", scrape_id, status_label
        )


