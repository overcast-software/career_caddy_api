"""Tighten the linkedin.com ScrapeProfile.extraction_hints gate that
emits "[DESCRIPTION NOT CAPTURED ...]" so it stops misfiring on rich
cc_sender extension captures.

The original hint (added 2026-05-03 after the scrape-298 SDUI partial-
render incident) told the LLM to emit the placeholder banner whenever
both sentinel phrases appeared in job_content:

  - "Use AI to assess how you fit"  (LinkedIn job-details top-card)
  - "Looking for talent?"           (LinkedIn site footer)

That gate is correct for Camoufox poller captures where only the top
card hydrated and the footer was injected by SDUI. But it misfires on
EVERY cc_sender extension capture of a /jobs/view/<id> page — the top
card always carries the AI button and the LinkedIn footer always
carries "Looking for talent?", with the full description sitting
BETWEEN them. Concrete regression: JP 2045 / scrape 422 (Stripe
Backend/API Engineer, ~9KB cc_sender capture). The scorer correctly
scored 82 from the real job_content body, but the extractor wrote
the placeholder into JobPost.description because the LLM dutifully
followed the (overfiring) hint.

The new hint requires BOTH sentinel phrases AND one of:

  - the body between the company-name marker and the LinkedIn footer
    is shorter than ~500 chars, OR
  - NONE of these structural tokens appear in the body:
    Responsibilities / Qualifications / About the job /
    What you'll do / Who you are / Minimum Requirements

If either tightening leg fires, real body content flows through and
the placeholder is NOT emitted.

Belt-and-suspenders for the same regression lives in
``JobPostExtractor._get_profile_hints``: when ``scrape.source ==
'extension'`` the hint block is skipped entirely (extension captures
are live DOM — partial-render is impossible).

Match policy: detect the known-bad substring fragments by presence
rather than full-text equality. The hint may have been hand-edited by
operators (sharpen endpoint, manual /admin/scrape-profiles edits),
so an exact-string match would silently no-op on every drifted copy.
Only the linkedin.com row is touched. Backwards-compatible reverse:
re-write the legacy phrasing the next deploy would have needed.
"""
from django.db import migrations


# Substrings that prove the legacy "presence-only" hint is in force.
# Both must appear to confirm we're looking at the partial-render hint
# (not some other hint about a different LinkedIn behavior).
_LEGACY_FINGERPRINT_TOKENS = (
    "Use AI to assess how you fit",
    "Looking for talent",
    "DESCRIPTION NOT CAPTURED",
)


# The tightened hint. Written in plain prose because the LLM consumes
# this verbatim alongside scrape.job_content; structural prose beats
# pseudocode for following-instructions reliability.
_TIGHTENED_HINT = """\
LinkedIn /jobs/view/<id> pages occasionally land in a partial-render \
state where ONLY the top card hydrated and the "About the job" subtree \
never appeared in the DOM. The fingerprint is the sentinel pair "Use \
AI to assess how you fit" (top-card AI button) AND "Looking for \
talent?" (LinkedIn site footer). When BOTH sentinels appear AND the \
captured body between the company-name marker and the LinkedIn footer \
is SHORTER than ~500 characters, AND none of the following structural \
tokens appear in the body — "Responsibilities", "Qualifications", \
"About the job", "What you'll do", "Who you are", "Minimum \
Requirements" — set description to exactly: "[DESCRIPTION NOT \
CAPTURED — LinkedIn page rendered only the top card; rescrape later \
or capture via the cc_sender extension]". Otherwise — if there IS \
real body content (long body OR any of the structural tokens above) \
— extract the description normally and DO NOT emit the placeholder. \
Rich extension captures carry the full body verbatim; presence of the \
sentinel phrases alone is NOT sufficient to declare partial-render.\
"""


def tighten_linkedin_extraction_hints(apps, schema_editor):
    ScrapeProfile = apps.get_model("job_hunting", "ScrapeProfile")
    profile = ScrapeProfile.objects.filter(hostname="linkedin.com").first()
    if not profile:
        return
    existing = profile.extraction_hints or ""
    # Idempotency: if the tightened wording is already in place (re-run
    # after partial rollback, or a hand-edit that landed the same gate),
    # no-op.
    if "Rich extension captures carry the full body" in existing:
        return
    # Apply only when the known-bad fingerprint is present. Without all
    # three tokens, the row holds a different hint shape (a hand-written
    # alternative, an empty string, or a drift we don't recognize) and
    # we leave it alone — the parser-side bypass on
    # ``scrape.source == 'extension'`` still mitigates the regression.
    if not all(tok in existing for tok in _LEGACY_FINGERPRINT_TOKENS):
        return
    profile.extraction_hints = _TIGHTENED_HINT
    profile.save(update_fields=["extraction_hints", "updated_at"])


def revert_linkedin_extraction_hints(apps, schema_editor):
    """Reverse: only touch the row if THIS migration's wording is
    still in place; do not invent legacy text on rollback.

    Operators who hand-edited the hint after the migration ran would
    get their text wiped on a blind revert. Safer to no-op and leave
    operators to author whatever wording they want at the next deploy.
    """
    ScrapeProfile = apps.get_model("job_hunting", "ScrapeProfile")
    profile = ScrapeProfile.objects.filter(hostname="linkedin.com").first()
    if not profile:
        return
    if profile.extraction_hints == _TIGHTENED_HINT:
        profile.extraction_hints = ""
        profile.save(update_fields=["extraction_hints", "updated_at"])


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0100_jobpost_reposted_from"),
    ]

    operations = [
        migrations.RunPython(
            tighten_linkedin_extraction_hints,
            revert_linkedin_extraction_hints,
        ),
    ]
