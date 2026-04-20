"""Daily activity time-series for the /reports/activity calendar.

Aggregates JobApplication creation dates into a dense day-by-day array
so a d3 calendar heatmap can render a GitHub-contribution-style grid
without client-side bucketing.
"""
from collections import Counter
from datetime import date, timedelta

from django.db.models.functions import TruncDate
from django.db.models import Count
from django.utils import timezone


def build_activity(applications_qs, *, from_date=None, to_date=None) -> dict:
    """Aggregate a JobApplication queryset into {date, count} entries.

    `from_date` / `to_date` are optional inclusive bounds; when either is
    omitted we default to the last 365 days ending today. The returned
    `days` list is dense (zeros for blank days in the range) so the
    frontend can index by date without gaps.
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

    return {
        "days": days,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "total_applications": total,
    }
