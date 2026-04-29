"""Tracker URL resolution for scrape ingest.

Marketing emails wrap real job-posting URLs in per-recipient click
trackers (SendGrid, Mailgun, LinkedIn /comm/, click.ziprecruiter.com,
etc.) Each tracker URL is unique per recipient but redirects to the
same destination, so dedupe against the raw submitted string misses
them. We HEAD-follow the redirect chain, validate the response, and
hand the resolved URL back to the caller for dedupe.

Coordinate with the cc_auto-side resolver at
``career_caddy_automation/src/agents/url_extractor.py`` so both
produce the same canonical form. If the tracker host list drifts
between the two repos, an email-pasted link and a user-pasted link
to the same destination dedupe inconsistently.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import requests

from job_hunting.models.job_post_dedupe import canonicalize_link

logger = logging.getLogger(__name__)

# Hostname suffixes that we treat as tracker domains. Match is case-
# insensitive and tail-anchored so subdomains hit
# (e.g. ``url9751.alerts.jobot.com`` matches ``alerts.jobot.com``).
_TRACKER_HOST_SUFFIXES = (
    "alerts.jobot.com",
    "click.ziprecruiter.com",
    "email.mg.indeed.com",
    "email.mg.linkedin.com",
    "email.mg.monster.com",
    "links.indeed.com",
    "click.indeed.com",
    "url.greenhouse.io",
    "click.appcast.io",
    "sg-mail.com",
    "sendgrid.net",
    "mailgun.org",
    "list-manage.com",
)

# LinkedIn's tracking paths (host stays linkedin.com but the URL is
# laden with trackingId/refId). Treated as redirect-resolvable when the
# path matches.
_TRACKER_PATH_PREFIXES_BY_HOST: dict[str, tuple[str, ...]] = {
    "linkedin.com": ("/comm/",),
    "www.linkedin.com": ("/comm/",),
}


_DEFAULT_TIMEOUT = 3.0


@dataclass
class TrackerResolution:
    resolved_url: Optional[str]
    status_code: Optional[int]
    error: Optional[str]

    @property
    def ok(self) -> bool:
        return (
            self.resolved_url is not None
            and self.status_code is not None
            and 200 <= self.status_code < 400
        )


def is_tracker_host(url: str) -> bool:
    """True if ``url``'s host (or path under a tracker host) matches a
    known tracker pattern.
    """
    if not url:
        return False
    try:
        parts = urlparse(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if not host:
        return False
    if any(host == s or host.endswith("." + s) for s in _TRACKER_HOST_SUFFIXES):
        return True
    prefixes = _TRACKER_PATH_PREFIXES_BY_HOST.get(host)
    if prefixes and any(parts.path.startswith(p) for p in prefixes):
        return True
    return False


def resolve_tracker(url: str, *, timeout: float = _DEFAULT_TIMEOUT) -> TrackerResolution:
    """Follow redirects on a tracker URL and return where it lands.

    Tries HEAD first (cheap), falls back to GET on 405. Result's
    ``resolved_url`` is canonicalized via
    ``job_post_dedupe.canonicalize_link`` so query-string trackers on
    the destination are stripped too. Caller decides what to do with
    a 4xx/5xx — typically reject the scrape.
    """
    try:
        resp = requests.head(url, allow_redirects=True, timeout=timeout)
        if resp.status_code == 405:
            resp = requests.get(
                url, allow_redirects=True, timeout=timeout, stream=True
            )
            resp.close()
    except requests.RequestException as exc:
        logger.warning(
            "resolve_tracker: request failed url=%s exc=%s", url, exc
        )
        return TrackerResolution(
            resolved_url=None,
            status_code=None,
            error=f"{type(exc).__name__}: {exc}",
        )

    landed = resp.url or url
    canonical = canonicalize_link(landed)
    return TrackerResolution(
        resolved_url=canonical,
        status_code=resp.status_code,
        error=None,
    )
