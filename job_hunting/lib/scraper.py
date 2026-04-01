import os
import threading
import requests
from typing import Optional


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

        If USE_A2A_BROWSER_AGENT=True, delegates to the A2A browser agent
        (BROWSER_AGENT_URL, default http://localhost:3012) instead.
        """
        if os.getenv("USE_A2A_BROWSER_AGENT", "").lower() in ("1", "true", "yes"):
            self._dispatch_a2a()
        else:
            self._dispatch_legacy()

    def _dispatch_legacy(self) -> None:
        payload = {"url": self.url}
        if self.scrape_id is not None:
            payload["scrape_id"] = self.scrape_id

        def _send():
            try:
                requests.post(
                    f"{self.browser_service_url}/scrape_job",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=300,
                )
            except Exception:
                pass

        thread = threading.Thread(target=_send, daemon=True)
        thread.start()

    def _dispatch_a2a(self) -> None:
        from job_hunting.lib.a2a_client import get_browser_agent_client

        url = self.url
        scrape_id = self.scrape_id

        def _send():
            try:
                client = get_browser_agent_client()
                message = f"Scrape this job posting URL and return the content as markdown: {url}"
                if scrape_id is not None:
                    message += f" (scrape_id: {scrape_id})"
                client.send(message)
            except Exception:
                pass

        thread = threading.Thread(target=_send, daemon=True)
        thread.start()
