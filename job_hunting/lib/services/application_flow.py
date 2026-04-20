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

# Two sequential hub layers between job_posts and the downstream
# branches. Every post flows through one scoring hub then one vetting
# hub. d3-sankey stacks same-depth nodes as a vertical column, so each
# hub renders like the Thermal-generation hub in
# https://observablehq.com/@d3/sankey/2.
#
# Scoring hub: scored / unscored (algorithmic step).
# Vetting hub: vetted_good / vetted_bad / unvetted (manual triage step).
# A job can be vetted without a score, so the two axes are independent.
NODE_SCORED = "scored"
NODE_UNSCORED = "unscored"
NODE_VETTED_GOOD = "vetted_good"
NODE_VETTED_BAD = "vetted_bad"
NODE_UNVETTED = "unvetted"

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

# Pre-application terminal under the unvetted hub: post has thin/empty
# description. Separates "email-pipeline junk" from "full description but
# never triaged".
BUCKET_STUB = "stub"
STUB_MIN_WORDS = 60

STAGE_BUCKETS = {BUCKET_APPLIED, BUCKET_INTERVIEW, BUCKET_OFFER}
TERMINAL_BUCKETS = {
    BUCKET_ACCEPTED,
    BUCKET_DECLINED,
    BUCKET_REJECTED,
    BUCKET_WITHDREW,
    BUCKET_GHOSTED,
}

# Status name → bucket. Case-insensitive match on Status.status.
#
# Pre-application triage labels (Unvetted, Vetted Good) are deliberately
# absent — they represent "I saw this post and it's worth considering",
# not "I applied". Applications whose only statuses are triage labels
# collapse to no_application in build_flow below.
BUCKETS: dict[str, str] = {
    # applied / applied-adjacent stages
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


def _is_thin_description(post) -> bool:
    """True when description is empty or has fewer than STUB_MIN_WORDS
    whitespace tokens. Used inside the unvetted branch to split 'thin
    junk' (→ stub) from 'full-description but never triaged' (→ rest)."""
    desc = (post.description or "").strip()
    return not desc or len(desc.split()) < STUB_MIN_WORDS


def _has_score(post, user_id: int | None) -> bool:
    scores = post.scores
    if user_id is not None:
        scores = scores.filter(user_id=user_id)
    return scores.exists()


def _vetting_hub(post, user_id: int | None) -> str:
    """Classify the post into one of the three triage hubs. Picks the most
    recent Vetted-Good / Vetted-Bad status across the post's applications
    (scoped to user_id when provided). Absence of either → unvetted."""
    apps = post.applications.all()
    if user_id is not None:
        apps = [a for a in apps if a.user_id == user_id]
    latest_good = None
    latest_bad = None
    for app in apps:
        status_rows = list(app.application_statuses.all())
        for s in status_rows:
            name = s.status.status if s.status_id else None
            ts = s.logged_at or s.created_at
            if name == "Vetted Good" and (latest_good is None or ts > latest_good):
                latest_good = ts
            elif name == "Vetted Bad" and (latest_bad is None or ts > latest_bad):
                latest_bad = ts
    if latest_good and (not latest_bad or latest_good >= latest_bad):
        return NODE_VETTED_GOOD
    if latest_bad:
        return NODE_VETTED_BAD
    return NODE_UNVETTED


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

        # Two sequential hub layers mid-graph. Each post flows
        # job_posts → vetting_hub → scoring_hub → terminal branch.
        # Vetting comes first because it's the manual triage step — you
        # decide whether to pursue before running a score. Jobs can be
        # vetted without ever being scored, so both axes are independent.
        vet_hub = _vetting_hub(post, user_id)
        score_hub = NODE_SCORED if _has_score(post, user_id) else NODE_UNSCORED
        edge_counts[(NODE_JOB_POSTS, vet_hub)] += 1
        edge_counts[(vet_hub, score_hub)] += 1
        hub = score_hub

        # Resolve each app's bucket sequence. Apps whose only statuses
        # are pre-application triage labels (Unvetted, Vetted Good —
        # deliberately unmapped in BUCKETS above) produce an empty
        # sequence and don't count as real applications — they're
        # "worth considering", not "applied". The post collapses to
        # no_application just like a post with no JobApplication row.
        real_apps = [(app, _app_bucket_sequence(app, now)) for app in apps]
        real_apps = [(app, seq) for app, seq in real_apps if seq]

        if not real_apps:
            # Stub terminal = thin-description post that's never been
            # scored AND never been triaged. Hangs off the scoring hub
            # since that's the downstream side of the chain.
            is_raw_junk = (
                vet_hub == NODE_UNVETTED
                and score_hub == NODE_UNSCORED
                and _is_thin_description(post)
            )
            if is_raw_junk:
                edge_counts[(hub, BUCKET_STUB)] += 1
            else:
                edge_counts[(hub, NODE_NO_APPLICATION)] += 1
            continue

        for _app, sequence in real_apps:
            total_apps += 1
            edge_counts[(hub, NODE_APPLICATIONS)] += 1
            prev = NODE_APPLICATIONS
            for bucket in sequence:
                edge_counts[(prev, bucket)] += 1
                prev = bucket

    # Stable ordering: keep the visual left-to-right the same each render.
    node_order = [
        NODE_JOB_POSTS,
        NODE_VETTED_GOOD,
        NODE_VETTED_BAD,
        NODE_UNVETTED,
        NODE_SCORED,
        NODE_UNSCORED,
        BUCKET_STUB,
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
