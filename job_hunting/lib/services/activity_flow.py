"""Daily activity time-series for the /reports/activity page.

Two visualizations share one payload:
  * calendar heatmap — days[].count from applied_at
  * stacked area chart — days[].by_bucket (applied / interview / offer /
    terminals) from JobApplicationStatus.logged_at. Same time axis, so
    the two chart components index the same `days` array.
"""
from collections import Counter
from datetime import date, timedelta

from django.db.models.functions import TruncDate
from django.db.models import Count
from django.utils import timezone

# Import the BUCKETS mapping so the stacked-area series uses the exact
# same status → bucket logic as the sankey. Keeping them in sync avoids
# a user applying via chat/API but seeing the sankey and activity
# reports disagree about whether that Status row counts as "applied".
from job_hunting.lib.services.application_flow import BUCKETS, IGNORE_STATUSES

# Stable render order for the stacked area chart — matches sankey's
# left→right flow (applied → interview → offer → terminals), with
# terminals ordered positive → negative so "good news" sits on top.
BUCKET_ORDER = [
    "applied",
    "interview",
    "offer",
    "accepted",
    "declined",
    "rejected",
    "withdrew",
    "ghosted",
]


def _status_series(status_rows, start, end):
    """Group JobApplicationStatus rows by (day, bucket) and return a
    dense list of per-day bucket counts. `status_rows` is a queryset that
    already has the user/scope filter applied; we filter by status name
    here using the shared BUCKETS map and skip rows in IGNORE_STATUSES.

    Uses TruncDate('logged_at') on the DB side so the day boundary
    respects the project's USE_TZ settings — avoids a UTC-vs-local-date
    mismatch where a late-evening log event lands on tomorrow's bucket.
    """
    empty_day = {b: 0 for b in BUCKET_ORDER}
    by_day: dict[date, dict[str, int]] = {}

    rows = (
        status_rows
        .annotate(day=TruncDate("logged_at"))
        .filter(day__gte=start, day__lte=end)
        .values("day", "status__status")
        .annotate(n=Count("id"))
    )
    for row in rows:
        name = row["status__status"]
        if not name or name in IGNORE_STATUSES:
            continue
        bucket = BUCKETS.get(name)
        if bucket is None or bucket not in empty_day:
            continue
        day = row["day"]
        if day is None:
            continue
        if day not in by_day:
            by_day[day] = empty_day.copy()
        by_day[day][bucket] += row["n"]

    days = []
    total = 0
    cursor = start
    while cursor <= end:
        entry = by_day.get(cursor, empty_day.copy())
        total += sum(entry.values())
        days.append({"date": cursor.isoformat(), **entry})
        cursor += timedelta(days=1)
    return {"days": days, "buckets": list(BUCKET_ORDER), "total_events": total}


def build_activity(
    applications_qs,
    *,
    status_rows=None,
    from_date=None,
    to_date=None,
) -> dict:
    """Aggregate a JobApplication queryset into {date, count} entries.

    `from_date` / `to_date` are optional inclusive bounds; when either is
    omitted we default to the last 365 days ending today. The returned
    `days` list is dense (zeros for blank days in the range) so the
    frontend can index by date without gaps.

    When `status_rows` (a JobApplicationStatus queryset, already scoped
    to the same user/tenant as applications_qs) is passed, the payload
    also includes a `status_series` key feeding the stacked-area chart.
    """
    today = timezone.localdate()
    end = to_date or today
    start = from_date or (end - timedelta(days=365))
    if start > end:
        start, end = end, start

    # JobApplication has no created_at; applied_at is the canonical
    # "when did I apply" timestamp. Applications still in triage (no
    # applied_at) don't register as a day-of-applying.
    rows = (
        applications_qs.filter(applied_at__isnull=False)
        .annotate(day=TruncDate("applied_at"))
        .values("day")
        .annotate(n=Count("id"))
        .filter(day__gte=start, day__lte=end)
    )
    counts: Counter[date] = Counter()
    for row in rows:
        d = row["day"]
        if d is not None:
            counts[d] += row["n"]

    days = []
    cursor = start
    total = 0
    while cursor <= end:
        n = counts.get(cursor, 0)
        days.append({"date": cursor.isoformat(), "count": n})
        total += n
        cursor += timedelta(days=1)

    payload = {
        "days": days,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "total_applications": total,
    }
    if status_rows is not None:
        payload["status_series"] = _status_series(status_rows, start, end)
    return payload
