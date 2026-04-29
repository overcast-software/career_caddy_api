"""Seed ScrapeProfile.apply_resolver_config for the hosts we scrape most.

Until a profile row carries a non-empty ``apply_resolver_config`` the
ResolveApplyUrl node no-ops with status=``unknown`` (same shape as
``url_rewrites``). This migration populates conservative defaults for
the hosts that show up in production, so freshly-scraped postings on
those hosts immediately resolve to either an internal-apply chip or a
real off-site URL.

Selectors are deliberately a small, stable subset — the goal is to land
*something* per host rather than an exhaustive list. Phase 3's learning
loop (apply_candidates capture on Scrape) feeds back into these via
follow-up migrations / admin edits as drift shows up.

Resolver tries: internal markers → link selectors → button selectors.
First match wins, so order within each list matters.
"""
from django.db import migrations


SEEDS = {
    # LinkedIn: Easy Apply is the dominant internal flow; off-site
    # postings expose a "company site" link.
    "linkedin.com": {
        "internal_apply_markers": [
            ".jobs-apply-button--easy-apply",
            "button[aria-label*='Easy Apply']",
        ],
        "apply_link_selectors": [
            "a.jobs-apply-link[href]",
            "a[data-tracking-control-name*='apply'][href^='http']",
        ],
        "apply_button_selectors": [],
    },
    # Greenhouse-hosted boards. The embed iframe + #apply_button mean the
    # apply flow stays on the same posting URL — internal from the
    # candidate's perspective.
    "greenhouse.io": {
        "internal_apply_markers": [
            "iframe#grnhse_iframe",
            "#apply_button",
            "a.template-btn-submit",
        ],
        "apply_link_selectors": [],
        "apply_button_selectors": [],
    },
    "boards.greenhouse.io": {
        "internal_apply_markers": [
            "iframe#grnhse_iframe",
            "#apply_button",
            "a.template-btn-submit",
        ],
        "apply_link_selectors": [],
        "apply_button_selectors": [],
    },
    # Lever postings: apply button is a real <a> with the apply path.
    "lever.co": {
        "internal_apply_markers": [],
        "apply_link_selectors": [
            "a.postings-btn[href*='/apply']",
            "a[href*='/apply']",
        ],
        "apply_button_selectors": [],
    },
    "jobs.lever.co": {
        "internal_apply_markers": [],
        "apply_link_selectors": [
            "a.postings-btn[href*='/apply']",
            "a[href*='/apply']",
        ],
        "apply_button_selectors": [],
    },
    # Jobot keeps applies on their own platform.
    "jobot.com": {
        "internal_apply_markers": [
            "button[data-test='apply-button']",
            "a[href*='jobot.com/apply']",
        ],
        "apply_link_selectors": [],
        "apply_button_selectors": [],
    },
    # Indeed: Indeed Apply when present (internal); else "Apply on
    # company site" link points off-platform.
    "indeed.com": {
        "internal_apply_markers": [
            "button#indeedApplyButton",
            "button[data-testid='indeedApplyButton']",
            "[data-indeed-apply-joburl]",
        ],
        "apply_link_selectors": [
            "a[data-testid='apply-button-link'][href^='http']",
            "a[id='applyButtonLinkContainer'] a[href^='http']",
        ],
        "apply_button_selectors": [],
    },
    # ZipRecruiter: most postings click through to a third-party ATS.
    "ziprecruiter.com": {
        "internal_apply_markers": [
            "button[data-name='apply_button']",
        ],
        "apply_link_selectors": [
            "a.apply_button[href^='http']",
            "a[data-name='apply_button'][href^='http']",
        ],
        "apply_button_selectors": [
            "button[data-name='apply_button']",
        ],
    },
}


def seed(apps, schema_editor):
    ScrapeProfile = apps.get_model("job_hunting", "ScrapeProfile")
    for hostname, config in SEEDS.items():
        profile, _ = ScrapeProfile.objects.get_or_create(hostname=hostname)
        # Don't trample an operator-tweaked config — only seed when empty.
        if profile.apply_resolver_config:
            continue
        profile.apply_resolver_config = config
        profile.save(update_fields=["apply_resolver_config"])


def unseed(apps, schema_editor):
    ScrapeProfile = apps.get_model("job_hunting", "ScrapeProfile")
    for hostname, config in SEEDS.items():
        profile = ScrapeProfile.objects.filter(hostname=hostname).first()
        if not profile:
            continue
        if profile.apply_resolver_config == config:
            profile.apply_resolver_config = None
            profile.save(update_fields=["apply_resolver_config"])


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0061_rename_application_status_to_posting_status"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
