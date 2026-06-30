"""BACK-111 — Tier-0 hit counter + forward-measured known-good backfill.

Adds ``ScrapeProfile.tier0_hit_count`` (the count of Tier-0 passes that ran
and MATCHED the required fields). Paired with the existing
``tier0_miss_count``, it yields a "Tier-0 attempts" denominator (hits +
misses) that excludes HTML-less scrapes — extension-direct / paste / email
scrapes never run a deterministic Tier-0 pass, yet they bumped
``scrape_count`` and so diluted the old miss ratio. That dilution let a host
whose CSS NEVER matches (talent.toptal.com: 6 Tier-0 misses, 0 hits, but 33
scrapes → 0.18 < 0.5) compute ``is_known_good=true``.

Backfill — forward measurement, NO retroactive guess:
    A stored counter cannot retroactively tell a dead-CSS host (toptal) from a
    live-CSS host with real hits (linkedin: high tier0_miss_count but many real
    matches) — both carry high success_rate. So we measure FORWARD. This
    migration RESETS ``tier0_miss_count`` to 0 for every existing row (the new
    ``tier0_hit_count`` starts at its 0 default), putting every host at 0
    Tier-0 attempts on deploy.

    Relearn window: with 0 attempts, ``tier0_attempts <
    KNOWN_GOOD_MIN_TIER0_ATTEMPTS`` (3) for every host, so the CSS-trust clause
    in ``ScrapeProfile.readiness`` does NOT fire on day 1 — no host is flipped
    out of known-good by this deploy (invariant a: no fleet-wide outage). Each
    host then re-accrues real hit/miss on subsequent Tier-0 runs and converges:
    toptal accrues only misses → demoted to not-known-good once it crosses the
    3-attempt floor (invariant b: toptal converges to not-known-good);
    linkedin / governmentjobs accrue hits → stay known-good. Hosts whose
    traffic is mostly HTML-less (extension-direct) relearn more slowly because
    only real Tier-0 passes move the counters — that is the intended behavior,
    not a regression.

The reset is irreversible (the old miss counts are not preserved); the
reverse path only drops the new column.
"""

from __future__ import annotations

from django.db import migrations, models


def reset_tier0_counters(apps, schema_editor):
    """Zero Tier-0 counters so every host starts the forward measurement neutral."""
    ScrapeProfile = apps.get_model("job_hunting", "ScrapeProfile")
    ScrapeProfile.objects.update(tier0_miss_count=0, tier0_hit_count=0)


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0131_scrape_claim_queue_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="scrapeprofile",
            name="tier0_hit_count",
            field=models.IntegerField(default=0),
        ),
        migrations.RunPython(
            reset_tier0_counters,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
