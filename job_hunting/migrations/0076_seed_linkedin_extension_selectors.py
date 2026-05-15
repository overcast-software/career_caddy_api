# Data migration: seed linkedin.com ScrapeProfile.extension_selectors with
# the selectors the browser extension previously had baked in popup.js, so
# ccsender 1.2.0+ can fetch them at submit time and the api becomes the
# single source of truth for per-host extraction config.

from django.db import migrations


LINKEDIN_EXTENSION_SELECTORS = {
    "apply_button_selectors": [
        "a.jobs-apply-button[href]",
        "a[data-test-job-apply-button][href]",
        'a[data-control-name="jobdetails_topcard_inapply"][href]',
    ],
    "canonical_link_selectors": [
        'meta[property="og:url"]',
    ],
    # Named protocol the extension's DECODERS registry knows how to run.
    # linkedin_safety_go strips the linkedin.com/safety/go/?url= wrapper
    # and returns the embedded ATS URL.
    "apply_url_decoder": "linkedin_safety_go",
}


def seed_linkedin(apps, schema_editor):
    ScrapeProfile = apps.get_model("job_hunting", "ScrapeProfile")
    profile, _ = ScrapeProfile.objects.get_or_create(
        hostname="linkedin.com",
        defaults={"extension_selectors": LINKEDIN_EXTENSION_SELECTORS},
    )
    # Don't clobber a hand-edited config if one already exists; only
    # populate when the field is null/empty.
    if not profile.extension_selectors:
        profile.extension_selectors = LINKEDIN_EXTENSION_SELECTORS
        profile.save(update_fields=["extension_selectors"])


def unseed_linkedin(apps, schema_editor):
    ScrapeProfile = apps.get_model("job_hunting", "ScrapeProfile")
    ScrapeProfile.objects.filter(hostname="linkedin.com").update(
        extension_selectors=None
    )


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0075_scrape_profile_extension_selectors"),
    ]

    operations = [
        migrations.RunPython(seed_linkedin, unseed_linkedin),
    ]
