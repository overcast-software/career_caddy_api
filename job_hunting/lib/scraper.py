"""Scrape lifecycle helpers.

Once contained the synchronous `Scraper` class that POSTed to a
browser-MCP HTTP endpoint and updated the Scrape row in a background
thread. That path is gone — every scrape now starts as `status=hold`
and the hold-poller (ai/scripts/hold_poller.py) drives extraction.
What remains here are the bookkeeping helpers that the views still
call to log status transitions and (post-extraction) parse content
into a JobPost.
"""
import logging

logger = logging.getLogger(__name__)


def _maybe_caddy_extract(scrape, force: bool = False) -> None:
    """Parse job_content and create JobPost + Company via JobPostExtractor.

    By default, skips when scrape.job_post_id is set (first-pass idempotency).
    Exception: if the linked post has a thin/empty description, parse anyway
    with force=True so the common \"click Run scrape on a stub to enrich it\"
    flow actually updates the description. Fully-populated posts remain
    idempotent — a repeat scrape is a no-op unless the caller passes
    force=True explicitly.
    """
    from job_hunting.lib.parsers.job_post_extractor import parse_scrape
    from job_hunting.lib.services.application_flow import STUB_MIN_WORDS
    if not scrape.job_content:
        return
    if scrape.job_post_id and not force:
        from job_hunting.models import JobPost
        linked = JobPost.objects.filter(pk=scrape.job_post_id).only("description").first()
        desc = (linked.description or "").strip() if linked else ""
        is_thin = not desc or len(desc.split()) < STUB_MIN_WORDS
        if not is_thin:
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
) -> None:
    """Append a ScrapeStatus audit record (and optionally bump Scrape.status).

    graph_node / graph_payload are populated by the scrape-graph
    runner's tracing mixin; legacy callers leave them None.

    update_scrape_status=False skips the =Scrape.status= write — used
    by graph-transition so per-node trace rows don't clobber the
    legacy terminal status ('completed' / 'failed') that the frontend
    polls on. Otherwise the shadow-mode pipeline lands on
    'resolveapplyurl' and UI spinners never terminate.
    """
    try:
        from job_hunting.models.scrape import Scrape
        from job_hunting.models.scrape_status import ScrapeStatus
        from job_hunting.models.status import Status
        from django.utils import timezone

        if update_scrape_status:
            Scrape.objects.filter(pk=scrape_id).update(status=status_label)
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


