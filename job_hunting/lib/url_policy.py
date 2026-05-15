"""Ingest URL policy — hard reject URLs that don't belong in our pipeline.

Single source of truth for "is this URL eligible for ingestion?" checks. Used
by the scrape views (POST /scrapes/, POST /scrapes/from-text/), the
hold-poller pre-fetch hook, and any future email/MCP ingest path.

POLICY (always-on, blocking) lives here. PROVENANCE (soft-verify the
(link, text) pair, opt-in) lives in lib/link_provenance.py — kept separate
because the trust posture is different.

Phase 0 checks:
  1. Scheme allowlist (http, https only)
  2. SELF_HOSTS blocklist (careercaddy.online + www variant)
  3. Private/loopback host blocklist (literals + suffix match)

Phase 2 will add DNS resolution to catch IP-literal bypasses.
"""

from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})

# Hosts that represent Career Caddy itself. Submitting our own domain as a
# "job posting" produces garbage and pollutes our index — block it. Operators
# can extend via the INGEST_BLOCKED_HOSTS env var (comma-separated).
SELF_HOSTS = frozenset({
    "careercaddy.online",
    "www.careercaddy.online",
})

PRIVATE_HOST_SUFFIXES = (
    ".local",
    ".internal",
    ".lan",
    ".localhost",
)

LOOPBACK_HOSTS = frozenset({"localhost"})

# Host classification for the cross-platform dedup `canonical_redirect`
# logic. When an extension submit produces a hint URL pointing at one of
# these ATS hosts and the submitted JP itself is on a job board, we route
# the user to the ATS JP — the deep listing is the canonical record.
# Match shape: exact host, or any subdomain (host.endswith("." + suffix)),
# plus the `ats.<anything>` subdomain pattern that covers per-company ATS
# instances (ats.rippling.com, ats.lyft.com, …).
ATS_HOST_SUFFIXES = frozenset({
    "greenhouse.io",
    "lever.co",
    "workday.com",
    "myworkdayjobs.com",
    "ashbyhq.com",
    "bamboohr.com",
    "breezy.hr",
    "workable.com",
    "jobvite.com",
    "recruitee.com",
    "icims.com",
})

JOB_BOARD_HOSTS = frozenset({
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "ziprecruiter.com",
})


def _normalize_host(url: str) -> str:
    """Return the lowercased host with leading www. stripped, or '' on parse failure."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def host_in_ats(url: str) -> bool:
    """True iff the URL's host is a known ATS (greenhouse/lever/workday/…),
    including ats.<anything> per-company subdomains."""
    host = _normalize_host(url)
    if not host:
        return False
    if host.startswith("ats."):
        return True
    return any(host == s or host.endswith("." + s) for s in ATS_HOST_SUFFIXES)


def host_in_jobboard(url: str) -> bool:
    """True iff the URL's host is a known job-board aggregator."""
    host = _normalize_host(url)
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in JOB_BOARD_HOSTS)


class UrlPolicyError(ValueError):
    """Raised when a submitted URL violates ingest policy.

    Carries a stable `code` suitable for JSON:API errors[].code so the
    client can branch on the failure mode without parsing the message.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _extra_blocked_hosts() -> frozenset[str]:
    raw = os.environ.get("INGEST_BLOCKED_HOSTS", "")
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


def _is_private_host(host: str) -> bool:
    """True for loopback names, *.local-style suffixes, and RFC1918/loopback IP literals."""
    host_l = host.lower()
    if host_l in LOOPBACK_HOSTS:
        return True
    if any(host_l.endswith(suffix) for suffix in PRIVATE_HOST_SUFFIXES):
        return True
    # IP literal? Reject loopback, private, link-local, reserved.
    try:
        ip = ipaddress.ip_address(host_l)
    except ValueError:
        return False
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_multicast
    )


def validate_submission_url(raw: str) -> str:
    """Validate a URL for ingestion.

    Returns the input string unchanged on success (callers may layer
    normalization separately). Raises UrlPolicyError on rejection — the
    `code` attribute is one of: blocked_scheme, blocked_self, blocked_private,
    blocked_malformed.
    """
    if not raw or not isinstance(raw, str):
        raise UrlPolicyError("blocked_malformed", "URL is required")

    try:
        parts = urlsplit(raw.strip())
    except ValueError as e:
        raise UrlPolicyError("blocked_malformed", f"URL could not be parsed: {e}")

    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UrlPolicyError(
            "blocked_scheme",
            f"Scheme {scheme or '(none)'!r} is not allowed; use http or https.",
        )

    host = (parts.hostname or "").lower()
    if not host:
        raise UrlPolicyError("blocked_malformed", "URL has no host.")

    if host in SELF_HOSTS or host in _extra_blocked_hosts():
        raise UrlPolicyError(
            "blocked_self",
            "This page is on Career Caddy itself — nothing to ingest.",
        )

    if _is_private_host(host):
        raise UrlPolicyError(
            "blocked_private",
            f"Host {host!r} is private/internal and cannot be ingested.",
        )

    return raw
