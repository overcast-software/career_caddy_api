"""Duplicate detection for JobPost.

Separate from the apply-resolver (``apply_url`` / ``apply_url_status``).
Dedupe reads ``link`` only; the resolver reads/writes ``apply_url`` only.
The two code paths do not share helpers.
"""
from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gh_src", "gh_jid",
    "lever-source", "lever-origin",
    "trk", "refid", "trackingid",
    "source", "src",
}

_WS = re.compile(r"\s+")


def canonicalize_link(url: str | None) -> str | None:
    """Strip known tracking query params; return None for falsy input.

    Opaque path tokens (e.g. ziprecruiter ``/ekm/<token>``) are left alone —
    the token IS the identifier on those hosts. Dedupe falls through to the
    content fingerprint in that case.
    """
    if not url:
        return None
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
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


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
