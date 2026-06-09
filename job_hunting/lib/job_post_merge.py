"""Field-merge policy for JobPost dedupe paths.

JobPost is universal: any link/fingerprint match in a create-style flow
returns the existing row, but the caller usually has fresh fields the
existing row may be missing (e.g. cc_auto's email pipeline knows the
company; the original scrape that seeded the row didn't). Without a
merge step the new association is silently dropped — the email-pipeline
Microsoft regression (2026-04-29) was exactly this shape.

Policy: fill NULL/empty fields from `attrs`; never overwrite a populated
field. Wrong cc_auto guesses (or stale scrape data) shouldn't clobber a
curated association — that's a separate, explicit operation (PATCH).
"""

DEDUPE_BACKFILL_FIELDS = (
    "company_id", "title", "description", "location", "remote",
    "salary_min", "salary_max", "posted_date", "extraction_date",
    "posting_status", "source",
)


def merge_empty_fields_from_attrs(post, attrs):
    """Mutate `post` in place: for each key in `DEDUPE_BACKFILL_FIELDS`,
    copy over from `attrs` only when the existing value is NULL/empty
    AND the incoming value is populated. Persists with a targeted
    `update_fields` save so concurrent writes don't get clobbered.

    Always bumps ``last_seen_at`` to ``timezone.now()`` regardless of
    whether any other field was written — this helper is the shared
    "dedupe-hit, merge what's new" entry point used by
    ``views/jobs.py:create()`` (link-exact and fingerprint branches),
    ``lib/parsers/job_post_extractor.py`` (post-scrape merge), and
    ``views/career_data.py`` (career-data import). Bumping here
    centralises the rolling-window contract on the canonical merge
    site so the three callers don't need to wire the bump separately.

    Returns the list of field names that were actually written (empty
    list when nothing other than ``last_seen_at`` changed)."""
    from django.utils import timezone

    update_fields = []
    for field in DEDUPE_BACKFILL_FIELDS:
        if field not in attrs:
            continue
        new_value = attrs[field]
        if new_value in (None, ""):
            continue
        current = getattr(post, field, None)
        if current in (None, ""):
            setattr(post, field, new_value)
            update_fields.append(field)
    # Always advance last_seen_at on a dedupe hit. Even when every
    # incoming field was already populated (no merge needed), the
    # row was re-seen and the rolling window should advance.
    post.last_seen_at = timezone.now()
    save_fields = update_fields + ["last_seen_at"]
    post.save(update_fields=save_fields)
    return update_fields
