"""Seed ScrapeProfile.css_selectors.job_data for linkedin.com.

The browser extension reads css_selectors.job_data on send (via the
extension-selectors endpoint extended in this PR) and runs each
{field: selector} against the active tab DOM, producing
``structured_prefill``. The api persists that dict on Scrape and
JobPostExtractor._try_prefill_extraction skips the LLM when title +
company_name are present.

Two selectors only — title + company_name. Description / location are
intentionally NOT seeded:

- description: LinkedIn's =.jobs-description= block omits header/footer
  context the LLM uses to disambiguate edge cases; sending it as a prior
  loses signal.
- location: LinkedIn's primary-description container is densely styled
  with hashed class names that rotate on releases — too brittle to seed
  blind, and missing this field doesn't block LLM-skip.

Both selectors are intentionally conservative:

- ``h1`` — LinkedIn's job-details page always renders the job title in
  the page's first h1. Semantic HTML; not a hashed class.
- ``a[href*='/company/']`` — the company name on a job page links to
  the company's /company/<slug>/ profile. Substring href match dodges
  the hashed-class rotation problem.

Merges into the existing css_selectors blob (preserves
``rememberme_candidates`` from 0059 and any probation-graduated keys).
"""
from django.db import migrations


LINKEDIN_JOB_DATA_SELECTORS = {
    "title": "h1",
    "company_name": "a[href*='/company/']",
}


def seed_linkedin_job_data(apps, schema_editor):
    ScrapeProfile = apps.get_model("job_hunting", "ScrapeProfile")
    profile, _ = ScrapeProfile.objects.get_or_create(hostname="linkedin.com")
    css = dict(profile.css_selectors or {})
    existing = css.get("job_data") or {}
    if isinstance(existing, dict) and (
        existing.get("title") or existing.get("company_name")
    ):
        # Probation gate or a prior hand-edit already populated job_data
        # — don't overwrite the operator's choice.
        return
    css["job_data"] = LINKEDIN_JOB_DATA_SELECTORS
    profile.css_selectors = css
    profile.save(update_fields=["css_selectors"])


def unseed_linkedin_job_data(apps, schema_editor):
    ScrapeProfile = apps.get_model("job_hunting", "ScrapeProfile")
    profile = ScrapeProfile.objects.filter(hostname="linkedin.com").first()
    if not profile:
        return
    css = dict(profile.css_selectors or {})
    if css.get("job_data") == LINKEDIN_JOB_DATA_SELECTORS:
        css.pop("job_data", None)
        profile.css_selectors = css
        profile.save(update_fields=["css_selectors"])


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0092_scrape_extension_prefill"),
    ]

    operations = [
        migrations.RunPython(seed_linkedin_job_data, unseed_linkedin_job_data),
    ]
