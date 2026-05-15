"""Refresh linkedin.com apply-button selectors after dogfood discovery
that the modern LinkedIn job DOM uses hashed atomic class names (no
`jobs-apply-button`, no `data-test-job-apply-button`). Anchor to the
accessibility contract (aria-label) and the safety/go wrapper href —
both are stable surfaces LinkedIn doesn't rotate on every release.

Idempotent on the apply_button_selectors key only — leaves canonical_link
and decoder untouched so a follow-on hand-edit of those fields survives.
"""

from django.db import migrations


REFRESHED_APPLY_BUTTON_SELECTORS = [
    'a[aria-label="Apply on company website"][href]',
    'a[aria-label^="Apply on" i][href]',
    'a[href*="linkedin.com/safety/go/"][target="_blank"]',
]

PREVIOUS_APPLY_BUTTON_SELECTORS = [
    "a.jobs-apply-button[href]",
    "a[data-test-job-apply-button][href]",
    'a[data-control-name="jobdetails_topcard_inapply"][href]',
]


def _update_apply_selectors(profile, new_selectors):
    cfg = dict(profile.extension_selectors or {})
    cfg["apply_button_selectors"] = new_selectors
    profile.extension_selectors = cfg
    profile.save(update_fields=["extension_selectors"])


def refresh(apps, schema_editor):
    ScrapeProfile = apps.get_model("job_hunting", "ScrapeProfile")
    profile = ScrapeProfile.objects.filter(hostname="linkedin.com").first()
    if not profile or not profile.extension_selectors:
        return
    _update_apply_selectors(profile, REFRESHED_APPLY_BUTTON_SELECTORS)


def revert(apps, schema_editor):
    ScrapeProfile = apps.get_model("job_hunting", "ScrapeProfile")
    profile = ScrapeProfile.objects.filter(hostname="linkedin.com").first()
    if not profile or not profile.extension_selectors:
        return
    _update_apply_selectors(profile, PREVIOUS_APPLY_BUTTON_SELECTORS)


class Migration(migrations.Migration):
    dependencies = [
        ("job_hunting", "0076_seed_linkedin_extension_selectors"),
    ]
    operations = [
        migrations.RunPython(refresh, revert),
    ]
