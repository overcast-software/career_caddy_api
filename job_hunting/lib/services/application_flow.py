"""Aggregate a JobPost queryset into a d3-sankey-shaped flow report.

Shape goal (matches the mockup):

    Job Posts ──┬── No Application (terminal)
                └── Applications ──┬── Applied ──┬── Interview ──┬── Offer ──┬── Accepted
                                   │             │               │           └── Declined
                                   │             │               └── Rejected
                                   │             └── Ghosted (derived)
                                   └── Rejected / Withdrew (terminal)

The single source of truth for status → bucket mapping lives here (BUCKETS).
"""
from collections import Counter
from datetime import timedelta
from django.utils import timezone


# Synthetic nodes (not backed by Status rows)
NODE_JOB_POSTS = "job_posts"
NODE_NO_APPLICATION = "no_application"
NODE_APPLICATIONS = "applications"

# Stage buckets (pass-through). An application sits in one of these until it
# reaches a terminal bucket or ages out to "ghosted".
BUCKET_APPLIED = "applied"
BUCKET_INTERVIEW = "interview"
BUCKET_OFFER = "offer"

# Terminal buckets
BUCKET_ACCEPTED = "accepted"
BUCKET_DECLINED = "declined"
BUCKET_REJECTED = "rejected"
BUCKET_WITHDREW = "withdrew"
BUCKET_GHOSTED = "ghosted"

STAGE_BUCKETS = {BUCKET_APPLIED, BUCKET_INTERVIEW, BUCKET_OFFER}
TERMINAL_BUCKETS = {
    BUCKET_ACCEPTED,
    BUCKET_DECLINED,
    BUCKET_REJECTED,
    BUCKET_WITHDREW,
    BUCKET_GHOSTED,
}

# Status name → bucket. Case-insensitive match on Status.status.
BUCKETS: dict[str, str] = {
    # applied / applied-adjacent stages
    "Unvetted": BUCKET_APPLIED,
    "Vetted Good": BUCKET_APPLIED,
    "Applied": BUCKET_APPLIED,
    "Submitted": BUCKET_APPLIED,
    "Contact": BUCKET_APPLIED,
    "Awaiting Decision": BUCKET_APPLIED,
    # interview
    "Phone Screen": BUCKET_INTERVIEW,
    "Interview Scheduled": BUCKET_INTERVIEW,
    "Interviewed": BUCKET_INTERVIEW,
    "Technical Test": BUCKET_INTERVIEW,
    # offer
    "Offer": BUCKET_OFFER,
    # terminals
    "Accepted": BUCKET_ACCEPTED,
    "Declined": BUCKET_DECLINED,
    "Rejected": BUCKET_REJECTED,
    "Vetted Bad": BUCKET_REJECTED,
    "Withdrawn": BUCKET_WITHDREW,
    "Withdrew": BUCKET_WITHDREW,
}

# Statuses we filter out — they clutter the funnel without adding signal.
IGNORE_STATUSES = {"Archived", "Expired"}

GHOST_AFTER_DAYS = 30


def _bucket_for(status_name: str | None) -> str | None:
    if not status_name:
        return None
    if status_name in IGNORE_STATUSES:
        return None
    return BUCKETS.get(status_name)


def _app_bucket_sequence(application, now) -> list[str]:
    """Return the per-application chronological bucket sequence, deduped.

    Includes a trailing `ghosted` bucket when the last logged stage is a
    pass-through and the last log is older than GHOST_AFTER_DAYS.
    """
    # Sort by logged_at if present, else created_at — mirrors the frontend
    # status-log component's ordering.
    statuses = sorted(
        list(application.application_statuses.all()),
        key=lambda s: s.logged_at or s.created_at,
    )
    sequence: list[str] = []
    last_logged = None
    for s in statuses:
        bucket = _bucket_for(s.status.status if s.status_id else None)
        if bucket is None:
            continue
        if sequence and sequence[-1] == bucket:
            continue  # dedupe consecutive duplicates
        sequence.append(bucket)
        last_logged = s.logged_at or s.created_at

    if sequence and sequence[-1] in STAGE_BUCKETS and last_logged:
        if now - last_logged > timedelta(days=GHOST_AFTER_DAYS):
            sequence.append(BUCKET_GHOSTED)
    return sequence


def build_flow(job_posts_qs, user_id: int | None = None, now=None) -> dict:
    """Aggregate a JobPost queryset into a sankey-shaped payload.

    `user_id`, when provided, restricts which applications on each post count
    (used for `scope=mine`). When None, all applications on each post count
    (used for staff `scope=all`).
    """
    if now is None:
        now = timezone.now()

    edge_counts: Counter[tuple[str, str]] = Counter()
    total_posts = 0
    total_apps = 0

    # Prefetching: let the caller shape the queryset; we walk attributes.
    for post in job_posts_qs:
        total_posts += 1
        apps = list(post.applications.all())
        if user_id is not None:
            apps = [a for a in apps if a.user_id == user_id]

        if not apps:
            edge_counts[(NODE_JOB_POSTS, NODE_NO_APPLICATION)] += 1
            continue

        for app in apps:
            total_apps += 1
            edge_counts[(NODE_JOB_POSTS, NODE_APPLICATIONS)] += 1
            sequence = _app_bucket_sequence(app, now)
            prev = NODE_APPLICATIONS
            for bucket in sequence:
                edge_counts[(prev, bucket)] += 1
                prev = bucket

    # Stable ordering: keep the visual left-to-right the same each render.
    node_order = [
        NODE_JOB_POSTS,
        NODE_NO_APPLICATION,
        NODE_APPLICATIONS,
        BUCKET_APPLIED,
        BUCKET_INTERVIEW,
        BUCKET_OFFER,
        BUCKET_GHOSTED,
        BUCKET_REJECTED,
        BUCKET_WITHDREW,
        BUCKET_DECLINED,
        BUCKET_ACCEPTED,
    ]

    used_nodes = {src for src, _ in edge_counts} | {dst for _, dst in edge_counts}
    nodes = [{"id": n} for n in node_order if n in used_nodes]
    name_to_index = {n["id"]: i for i, n in enumerate(nodes)}
    links = [
        {"source": name_to_index[src], "target": name_to_index[dst], "value": value}
        for (src, dst), value in edge_counts.items()
        if src in name_to_index and dst in name_to_index
    ]
    return {
        "nodes": nodes,
        "links": links,
        "total_job_posts": total_posts,
        "total_applications": total_apps,
    }
