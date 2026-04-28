"""Seed ScrapeProfile.css_selectors.rememberme_candidates for linkedin.com.

Architectural: site-specific selectors live in ScrapeProfile.css_selectors,
not hardcoded in ai/. This migration moves the LinkedIn rememberme tile
selectors out of `_try_rememberme_reauth`'s in-code candidate list and
into per-host config so future hosts can be added by writing data
without redeploying ai.

The two LinkedIn surfaces:
- /uas/login rememberme tile: button.member-profile__details with
  aria-label="Login as <name>". Visible text is the user's name + email
  — no "continue" wording, which is why the old in-code heuristic
  whiffed here (scrape 186 dropped to ObstacleAgent and timed out).
- In-app session-restore: data-tracking-control-name='rememberme'
  buttons + "Continue as <name>" text — kept for older flows.

If a profile row already exists for linkedin.com, merge the new key
into its css_selectors without overwriting unrelated keys (e.g. an
existing obstacle_click_selector graduated by the probation gate).
"""
from django.db import migrations


LINKEDIN_REMEMBERME_CANDIDATES = [
    "button.member-profile__details",
    "button[aria-label^='Login as']",
    "button[data-tracking-control-name*='rememberme']",
    "a[data-tracking-control-name*='rememberme']",
    "button:has-text('Continue as')",
    "a:has-text('Continue as')",
]


def seed_linkedin_rememberme(apps, schema_editor):
    ScrapeProfile = apps.get_model("job_hunting", "ScrapeProfile")
    profile, _ = ScrapeProfile.objects.get_or_create(hostname="linkedin.com")
    css = dict(profile.css_selectors or {})
    css["rememberme_candidates"] = LINKEDIN_REMEMBERME_CANDIDATES
    profile.css_selectors = css
    profile.save(update_fields=["css_selectors"])


def unseed_linkedin_rememberme(apps, schema_editor):
    ScrapeProfile = apps.get_model("job_hunting", "ScrapeProfile")
    profile = ScrapeProfile.objects.filter(hostname="linkedin.com").first()
    if not profile:
        return
    css = dict(profile.css_selectors or {})
    css.pop("rememberme_candidates", None)
    profile.css_selectors = css
    profile.save(update_fields=["css_selectors"])


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0058_scrape_apply_candidates"),
    ]

    operations = [
        migrations.RunPython(seed_linkedin_rememberme, unseed_linkedin_rememberme),
    ]
