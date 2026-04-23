import os
import threading
import requests
from typing import Optional
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
) -> None:
    """Update Scrape.status AND append a ScrapeStatus audit record.

    graph_node / graph_payload are populated by the scrape-graph
    runner's tracing mixin; legacy callers leave them None.
    """
    try:
        from job_hunting.models.scrape import Scrape
        from job_hunting.models.scrape_status import ScrapeStatus
        from job_hunting.models.status import Status
        from django.utils import timezone

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


class Scraper:
    def __init__(self, browser_service_url: str, url: str, scrape_id: Optional[int] = None):
        self.url = url
        self.browser_service_url = browser_service_url
        self.scrape_id = scrape_id

    def dispatch(self) -> None:
        """
        Fire-and-forget: send the scrape request to the browser service in a
        background thread and return immediately.  The browser service is
        responsible for updating the scrape record (via scrape_id) when it
        finishes.

        If USE_MCP_BROWSER_AGENT=True, delegates to the MCP browser agent
        (BROWSER_MCP_SSE_URL, default http://0.0.0.0:3004/sse) instead.
        If USE_A2A_BROWSER_AGENT=True, delegates to the A2A browser agent
        (BROWSER_AGENT_URL, default http://localhost:3012) instead.
        """
        use_mcp = os.getenv("USE_MCP_BROWSER_AGENT", "").lower() in ("1", "true", "yes")
        use_a2a = os.getenv("USE_A2A_BROWSER_AGENT", "").lower() in ("1", "true", "yes")
        mode = "mcp" if use_mcp else ("a2a" if use_a2a else "legacy")
        logger.info("Scraper.dispatch mode=%s url=%s scrape_id=%s", mode, self.url, self.scrape_id)

        if use_mcp:
            self._dispatch_mcp()
        elif use_a2a:
            self._dispatch_a2a()
        else:
            self._dispatch_legacy()

    def _dispatch_legacy(self) -> None:
        def _send():
            try:
                if self.scrape_id is not None:
                    _set_scrape_status(self.scrape_id, "running")
                endpoint = f"{self.browser_service_url}/scrape_job"
                logger.info("Legacy dispatch -> POST %s (url=%s, scrape_id=%s)", endpoint, self.url, self.scrape_id)
                payload = {"url": self.url}
                if self.scrape_id is not None:
                    payload["scrape_id"] = self.scrape_id
                logger.debug("Legacy payload=%s", payload)
                resp = requests.post(
                    endpoint,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=300,
                )
                logger.info("Legacy dispatch response status=%s", getattr(resp, "status_code", None))
            except Exception:
                logger.exception("Legacy dispatch failed")
                if self.scrape_id is not None:
                    _set_scrape_status(self.scrape_id, "failed")

        thread = threading.Thread(target=_send, daemon=True)
        thread.start()

    def _dispatch_mcp(self) -> None:
        """
        Dispatch via MCP server 'browser-server' over SSE.
        Endpoint default: http://0.0.0.0:3004/sse (configurable via BROWSER_MCP_SSE_URL)
        Tool default: 'scrape' (configurable via BROWSER_MCP_TOOL_NAME)
        """
        from job_hunting.lib.mcp_client import get_browser_mcp_client

        url = self.url
        scrape_id = self.scrape_id

        def _send():
            try:
                if scrape_id is not None:
                    _log_scrape_status(scrape_id, "running")
                sse_url = os.getenv("BROWSER_MCP_SSE_URL", "http://0.0.0.0:3004/sse")
                logger.info("MCP dispatch -> SSE %s (url=%s, scrape_id=%s)", sse_url, url, scrape_id)
                client = get_browser_mcp_client()
                job_content = client.scrape(url, scrape_id)
                logger.info("MCP dispatch received response len=%s url=%s scrape_id=%s", len(job_content) if job_content else 0, url, scrape_id)
                if job_content:
                    from job_hunting.lib.scrapers.html_cleaner import strip_agent_chat
                    job_content = strip_agent_chat(job_content)
                    logger.info("MCP dispatch stripped response len=%s url=%s scrape_id=%s", len(job_content), url, scrape_id)
                if scrape_id is not None and job_content:
                    try:
                        from django.utils import timezone
                        from job_hunting.models.scrape import Scrape
                        _log_scrape_status(scrape_id, "scraping", note=f"Content received ({len(job_content)} chars)")
                        scrape = Scrape.objects.filter(pk=scrape_id).first()
                        if scrape:
                            scrape.job_content = job_content
                            scrape.scraped_at = timezone.now()
                            scrape.save(update_fields=["job_content", "scraped_at"])
                            logger.info("MCP dispatch: stored job_content on scrape id=%s", scrape_id)
                            _maybe_caddy_extract(scrape)
                    except Exception:
                        logger.exception("MCP dispatch: failed to store job_content scrape_id=%s", scrape_id)
                elif scrape_id is not None:
                    _log_scrape_status(scrape_id, "failed", note="No content returned from browser")
                    logger.warning("MCP dispatch: no content returned url=%s scrape_id=%s", url, scrape_id)
            except Exception:
                logger.exception("MCP dispatch failed")
                if scrape_id is not None:
                    _log_scrape_status(scrape_id, "failed", note="MCP dispatch exception")

        thread = threading.Thread(target=_send, daemon=True)
        thread.start()

    def _dispatch_a2a(self) -> None:
        from job_hunting.lib.a2a_client import get_browser_agent_client

        url = self.url
        scrape_id = self.scrape_id

        def _send():
            try:
                if scrape_id is not None:
                    _set_scrape_status(scrape_id, "running")
                client = get_browser_agent_client()
                message = f"Scrape this job posting URL and return the content as markdown: {url}"
                if scrape_id is not None:
                    message += f" (scrape_id: {scrape_id})"
                logger.info("A2A dispatch -> sending message (len=%s) url=%s scrape_id=%s", len(message), url, scrape_id)
                job_content = client.send(message)
                logger.info("A2A dispatch received response len=%s url=%s scrape_id=%s", len(job_content) if job_content else 0, url, scrape_id)
                if job_content:
                    from job_hunting.lib.scrapers.html_cleaner import strip_agent_chat
                    job_content = strip_agent_chat(job_content)
                    logger.info("A2A dispatch stripped response len=%s url=%s scrape_id=%s", len(job_content), url, scrape_id)
                if scrape_id is not None and job_content:
                    try:
                        from django.utils import timezone
                        from job_hunting.models.scrape import Scrape
                        scrape = Scrape.objects.filter(pk=scrape_id).first()
                        if scrape:
                            scrape.job_content = job_content
                            scrape.status = "completed"
                            scrape.scraped_at = timezone.now()
                            scrape.save(update_fields=["job_content", "status", "scraped_at"])
                            logger.info("A2A dispatch: stored job_content on scrape id=%s", scrape_id)
                            _maybe_caddy_extract(scrape)
                    except Exception:
                        logger.exception("A2A dispatch: failed to store job_content scrape_id=%s", scrape_id)
            except Exception:
                logger.exception("A2A dispatch failed")
                if scrape_id is not None:
                    _set_scrape_status(scrape_id, "failed")

        thread = threading.Thread(target=_send, daemon=True)
        thread.start()
