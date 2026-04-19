"""Aggregate JobPosts into a per-hostname × outcome-bucket rollup for the
`/reports/sources` horizontal stacked bar.

Groups posts by URL hostname (deterministic, unlike LLM-extracted company
names) and counts each post once by its latest outcome bucket. Reuses the
bucket map from `application_flow` — single source of truth.
"""
from collections import Counter
from datetime import timedelta
from urllib.parse import urlparse

from django.utils import timezone

from .application_flow import (
    BUCKETS,
    GHOST_AFTER_DAYS,
    STAGE_BUCKETS,
    BUCKET_GHOSTED,
)


# Synthetic hostname for posts with no / unparseable link.
HOSTNAME_DIRECT = "(direct)"

# Synthetic bucket when the user hasn't applied / triaged at all yet.
BUCKET_NO_APPLICATION = "no_application"

# Fallback bucket when the latest status doesn't match any known bucket —
# e.g. a Status row we don't classify yet. Keep it visible so we notice.
BUCKET_UNKNOWN = "unknown"

# Canonical rendering order: stages left-to-right, terminals after, noise last.
BUCKET_ORDER = [
    "applied",
    "interview",
    "offer",
    "accepted",
    "declined",
    "rejected",
    "withdrew",
    BUCKET_GHOSTED,
    BUCKET_NO_APPLICATION,
    BUCKET_UNKNOWN,
]

TOP_N_HOSTNAMES = 15
OTHER_HOSTNAME = "Other"


def hostname_of(url: str | None) -> str:
    if not url:
        return HOSTNAME_DIRECT
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return HOSTNAME_DIRECT
    if not host:
        return HOSTNAME_DIRECT
    if host.startswith("www."):
        host = host[4:]
    return host


def _latest_bucket_for_application(application, now) -> str:
    """Single bucket representing the application's current outcome."""
    statuses = sorted(
        list(application.application_statuses.all()),
        key=lambda s: s.logged_at or s.created_at,
    )
    last_bucket = None
    last_logged = None
    for s in statuses:
        name = s.status.status if s.status_id else None
        bucket = BUCKETS.get(name) if name else None
        if not bucket:
            continue
        last_bucket = bucket
        last_logged = s.logged_at or s.created_at
    if last_bucket is None:
        return BUCKET_UNKNOWN
    if last_bucket in STAGE_BUCKETS and last_logged:
        if now - last_logged > timedelta(days=GHOST_AFTER_DAYS):
            return BUCKET_GHOSTED
    return last_bucket


def _post_bucket(post, user_id: int | None, now) -> str:
    apps = list(post.applications.all())
    if user_id is not None:
        apps = [a for a in apps if a.user_id == user_id]
    if not apps:
        return BUCKET_NO_APPLICATION
    # Pick the most recently touched application (matches user intuition
    # when someone has multiple attempts on the same post).
    def _recency(a):
        latest = None
        for s in a.application_statuses.all():
            t = s.logged_at or s.created_at
            if latest is None or (t and t > latest):
                latest = t
        return latest or timezone.make_aware(timezone.datetime.min)
    apps.sort(key=_recency, reverse=True)
    return _latest_bucket_for_application(apps[0], now)


def build_sources(job_posts_qs, user_id: int | None = None, now=None) -> dict:
    """Aggregate a JobPost queryset into a hostname × bucket rollup.

    Returns a payload shaped for the frontend stacked-bar component:
        {
          "rows": [
            {"hostname": "linkedin.com", "total": 80, "buckets": {...}},
            ...
          ],
          "bucket_order": [...],
          "total_job_posts": N,
          "scope": "mine" | "all"  (set by the view)
        }
    """
    if now is None:
        now = timezone.now()

    host_bucket_counts: dict[str, Counter[str]] = {}
    host_totals: Counter[str] = Counter()
    total = 0

    for post in job_posts_qs:
        total += 1
        host = hostname_of(post.link)
        bucket = _post_bucket(post, user_id, now)
        host_totals[host] += 1
        host_bucket_counts.setdefault(host, Counter())[bucket] += 1

    # Sort hosts by total desc, tiebreak on name for stability.
    sorted_hosts = sorted(
        host_totals.keys(), key=lambda h: (-host_totals[h], h)
    )
    top = sorted_hosts[:TOP_N_HOSTNAMES]
    tail = sorted_hosts[TOP_N_HOSTNAMES:]

    rows: list[dict] = []
    for host in top:
        rows.append({
            "hostname": host,
            "total": host_totals[host],
            "buckets": dict(host_bucket_counts[host]),
        })
    if tail:
        other_buckets: Counter[str] = Counter()
        other_total = 0
        for host in tail:
            other_total += host_totals[host]
            other_buckets.update(host_bucket_counts[host])
        rows.append({
            "hostname": OTHER_HOSTNAME,
            "total": other_total,
            "buckets": dict(other_buckets),
        })

    return {
        "rows": rows,
        "bucket_order": BUCKET_ORDER,
        "total_job_posts": total,
    }
