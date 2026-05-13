"""Duplicate detection for JobPost.

Separate from the apply-resolver (``apply_url`` / ``apply_url_status``).
Dedupe reads ``link`` only; the resolver reads/writes ``apply_url`` only.
The two code paths do not share helpers.
"""
from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from job_hunting.lib.url_canonicalize import apply_url_rewrites


# Source-of-truth ranking. Higher value beats lower when two writes for
# the same canonical_link / fingerprint collide. The extension is treated
# as the most-authoritative because the user verified the page in their
# own browser before pushing it; email-pipeline writes are LLM-extracted
# from third-party digests and routinely hallucinate (the jp 1724 SNBL
# incident). When a higher-trust source lands on top of a lower-trust
# existing post, we overwrite the post in place and write a
# JobPostOverwriteDecision audit row.
SOURCE_TRUST = {
    "extension": 100,
    "paste": 80,
    "scrape": 70,
    "redirect": 60,
    "manual": 50,
    "email": 20,
    "email_direct": 20,
}


def source_trust(source: str | None) -> int:
    """Return a trust score for a JobPost source. Unknown → manual (50)."""
    return SOURCE_TRUST.get(source or "manual", SOURCE_TRUST["manual"])


_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gh_src", "gh_jid",
    "lever-source", "lever-origin",
    "trk", "refid", "trackingid",
    "lk", "lvk", "tsid",
}
# Note: bare =source= and =src= were here previously but stripping them
# overreaches — many job boards use =?source=...= as a *functional*
# query param (worksourcewa.com encodes part of the job identifier
# there). Without them in the strip list, /jobview/?source=A and
# /jobview/?source=B canonicalize to distinct URLs as intended.

_WS = re.compile(r"\s+")


@lru_cache(maxsize=256)
def _profile_url_rewrites_for_host(host: str) -> tuple:
    """Return the host's `url_rewrites` rules as a hashable tuple.

    Cached per-process. Profile edits won't be picked up until restart —
    acceptable today (rewrites change rarely, and prod restarts on every
    deploy). If that becomes a problem, swap to a TTL cache or wire a
    post_save signal on ScrapeProfile to call `cache_clear()`.

    Reads both top-level `url_rewrites` and `css_selectors.url_rewrites`
    because some legacy profiles authored the rules inside `css_selectors`
    (the agents-side LoadProfile flattens that blob); top-level wins when
    both exist and are non-empty.
    """
    if not host:
        return ()
    try:
        from job_hunting.models.scrape_profile import ScrapeProfile
        profile = ScrapeProfile.objects.filter(hostname=host).first()
    except Exception:
        return ()
    if not profile:
        return ()
    rules = profile.url_rewrites
    if not rules and isinstance(profile.css_selectors, dict):
        rules = profile.css_selectors.get("url_rewrites")
    if not isinstance(rules, list) or not rules:
        return ()
    return tuple(
        (r.get("match"), r.get("rewrite"))
        for r in rules
        if isinstance(r, dict) and r.get("match") and r.get("rewrite") is not None
    )


def _rewrite_via_profile(url: str) -> str:
    """Apply the host's ScrapeProfile.url_rewrites to `url`, if any."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return url
    if host.startswith("www."):
        host = host[4:]
    rules_tuple = _profile_url_rewrites_for_host(host)
    if not rules_tuple:
        return url
    rules = [{"match": m, "rewrite": r} for m, r in rules_tuple]
    return apply_url_rewrites(url, rules)


def canonicalize_link(url: str | None) -> str | None:
    """Apply host-specific path rewrites then strip tracking query params.

    Two-stage:
    1. Look up `ScrapeProfile.url_rewrites` for the URL's host and apply
       the first matching regex rule. This collapses host-specific URL
       variants (e.g. LinkedIn `/comm/jobs/view/` → `/jobs/view/`) onto a
       single canonical form so dedup recognises them as the same job.
    2. Strip known tracking query params (utm_*, gh_*, etc.) and the
       fragment. Opaque path tokens (e.g. ziprecruiter `/ekm/<token>`)
       are left alone — the token IS the identifier on those hosts.

    Returns None for falsy input.
    """
    if not url:
        return None
    url = _rewrite_via_profile(url)
    try:
        u = urlparse(url)
    except ValueError:
        return url
    kept = [
        (k, v)
        for k, v in parse_qsl(u.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    return urlunparse(u._replace(query=urlencode(kept), fragment=""))


def fingerprint(post) -> str | None:
    """sha1(company_id | normalized_title | normalized_location).

    Returns None when company_id or title is missing — null-fingerprint
    rows skip dedupe. Description is intentionally NOT hashed: recruiter
    tweaks break the match without changing the underlying role.
    """
    if not (getattr(post, "company_id", None) and post.title):
        return None
    parts = [
        str(post.company_id),
        _WS.sub(" ", post.title.strip().lower()),
        _WS.sub(" ", (post.location or "").strip().lower()),
    ]
    return hashlib.sha1(
        "|".join(parts).encode(), usedforsecurity=False
    ).hexdigest()


def find_duplicate(post, window_days: int = 30):
    """Return an existing JobPost this one duplicates, or None.

    Called before insert from creation paths. Also safe to call after a
    scrape enriches a previously-null-fingerprint row — the hold-poller
    uses it for a post-scrape re-check.
    """
    from django.utils import timezone

    from .job_post import JobPost

    if post.canonical_link:
        hit = (
            JobPost.objects
            .filter(canonical_link=post.canonical_link)
            .exclude(pk=post.pk)
            .order_by("created_at")
            .first()
        )
        if hit:
            return hit.canonical

    if post.content_fingerprint:
        cutoff = timezone.now() - timedelta(days=window_days)
        hit = (
            JobPost.objects
            .filter(
                content_fingerprint=post.content_fingerprint,
                created_at__gte=cutoff,
            )
            .exclude(pk=post.pk)
            .order_by("created_at")
            .first()
        )
        if hit:
            return hit.canonical

    return None
