"""Shared `?sort=` parsing + whitelisting for JSON:API list endpoints.

Centralizes the sort-param handling so every list view validates the
user-supplied field names against a per-resource whitelist BEFORE handing
them to ``QuerySet.order_by``. Without the whitelist an unknown field (e.g.
``?sort=-updated_at`` on JobPost, which has no ``updated_at`` column) sails
straight into ``order_by(F(name)...)`` and only blows up later at
``.count()`` / queryset materialization as a Django ``FieldError`` ->
unhandled 500 (CC-93). Raising :class:`InvalidSortField` lets the view
return a clean 400 with a JSON:API error pointing at the ``sort`` parameter.
"""
from django.db.models import F


class InvalidSortField(ValueError):
    """A ``sort`` query param referenced a field outside the resource whitelist."""

    def __init__(self, field, allowed):
        self.field = field
        self.allowed = sorted(allowed)
        super().__init__(
            f"Unsupported sort field '{field}'. "
            f"Allowed sort fields: {', '.join(self.allowed)}."
        )


def parse_sort_fields(sort_param, allowed, *, tiebreak="id", tiebreak_desc=True):
    """Parse a JSON:API ``sort`` value into validated order_by expressions.

    ``sort_param`` -- the raw comma-separated value (e.g. ``"-posted_date,title"``).
    ``allowed``    -- iterable of bare field names this resource permits sorting on.
    ``tiebreak``   -- appended as a deterministic final key when the user's sort
                      doesn't already pin it (pass ``None`` to disable).

    Returns a list of ``F(...).asc/desc(nulls_last=True)`` expressions suitable
    for ``QuerySet.order_by(*result)``. Raises :class:`InvalidSortField` on the
    first field not present in ``allowed`` so the caller can return HTTP 400
    instead of leaking a 500.
    """
    allowed = set(allowed)
    sort_fields = []
    seen_names = set()
    for raw in sort_param.split(","):
        field = raw.strip()
        if not field:
            continue
        name = field.lstrip("-")
        if name not in allowed:
            raise InvalidSortField(name, allowed)
        seen_names.add(name)
        if field.startswith("-"):
            sort_fields.append(F(name).desc(nulls_last=True))
        else:
            sort_fields.append(F(name).asc(nulls_last=True))
    # Deterministic tiebreak so rows sharing the user's sort-key value don't
    # reshuffle across pages (e.g. many posts sharing today's posted_date).
    if sort_fields and tiebreak and tiebreak not in seen_names:
        tb = F(tiebreak)
        sort_fields.append(tb.desc() if tiebreak_desc else tb.asc())
    return sort_fields


def sort_error_response_body(exc):
    """Build the JSON:API 400 error body for an :class:`InvalidSortField`."""
    return {
        "errors": [
            {
                "status": "400",
                "title": "Invalid sort field",
                "detail": str(exc),
                "source": {"parameter": "sort"},
            }
        ]
    }
