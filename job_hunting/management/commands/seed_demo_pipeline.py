"""
Management command: seed_demo_pipeline

Seeds the *guest* user with a rich, eye-catching application funnel so
the public /reports/application-flow sankey has something worth looking
at for anonymous visitors. Every sankey bucket is populated; a wide
set of hostnames drives the sources stacked bar; descriptions are
verbose enough to clear the STUB_MIN_WORDS=60 threshold (except for
deliberate stubs in the stub bucket).

Idempotent with `--reset`: clears prior demo posts (title prefix
`Demo:`) first. Without `--reset` it appends. Requires the `guest`
user to exist — run `seed_guest` first.
"""
import random
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from job_hunting.models import (
    Company,
    JobApplication,
    JobApplicationStatus,
    JobPost,
    Score,
    Status,
)

User = get_user_model()

# Funnel shape. Tuned so the anonymous sankey looks like a healthy
# pipeline — plenty at the top, meaningful flow through every stage,
# clear offer/accepted outcomes so the story lands.
FUNNEL_SHAPE = {
    "applied": 28,
    "interview": 18,
    "offer": 7,
    "accepted": 3,
    "declined": 2,
    "rejected": 12,
    "withdrew": 5,
    "ghosted": 9,
    "no_application": 22,
    "stub": 14,
}

# Status-name paths for each terminal bucket. Mirrors BUCKETS in
# job_hunting.lib.services.application_flow. Numbers are "days ago".
# Tuned so the full pipeline history fits inside the last 30 days,
# with ghosted pushed just past GHOST_AFTER_DAYS=30 so the sankey
# still derives that bucket.
PATHS = {
    "applied":     [("Applied", 5)],
    "interview":   [("Applied", 18), ("Phone Screen", 12), ("Interview Scheduled", 5)],
    "offer":       [("Applied", 26), ("Phone Screen", 19), ("Interviewed", 12), ("Offer", 3)],
    "accepted":    [("Applied", 28), ("Phone Screen", 22), ("Interviewed", 14), ("Offer", 7), ("Accepted", 2)],
    "declined":    [("Applied", 28), ("Phone Screen", 22), ("Interviewed", 14), ("Offer", 7), ("Declined", 2)],
    "rejected":    [("Applied", 16), ("Rejected", 7)],
    "withdrew":    [("Applied", 20), ("Withdrawn", 9)],
    # Ghosted needs a log entry older than GHOST_AFTER_DAYS=30 with no
    # follow-up. Keep it just past the threshold so the bucket pops but
    # the event still feels recent.
    "ghosted":     [("Applied", 38)],
}

# Mix of provenance values — the Sources stacked bar colours each
# segment of each hostname by bucket, so we want several sources so
# the legend does real work.
SOURCES = [
    "email", "email", "email", "email",
    "scrape", "scrape", "scrape",
    "paste", "paste",
    "manual",
    "chat",
    "import",
]

# Varied hostnames so the sources report has something to rank.
HOSTNAMES = [
    "greenhouse.io", "lever.co", "workable.com", "ashbyhq.com",
    "bamboohr.com", "smartrecruiters.com", "jobvite.com",
    "linkedin.com", "indeed.com", "builtin.com", "angel.co",
    "ycombinator.com", "otta.com", "wellfound.com",
    "careers.meridian.example", "jobs.copper-canyon.example",
    "apply.sycamore-ai.example", "careers.harbor-and-main.example",
    "jobs.verdant.example", "apply.lantern-systems.example",
]

COMPANIES = [
    ("Bluebird Labs",            "bluebirdlabs.example"),
    ("Meridian Health",          "meridianhealth.example"),
    ("Copper Canyon Robotics",   "coppercanyon.example"),
    ("Northwind Analytics",      "northwind-analytics.example"),
    ("Sycamore AI",              "sycamore-ai.example"),
    ("Ridgeline Payments",       "ridgelinepayments.example"),
    ("Turnstile Data",           "turnstiledata.example"),
    ("Forge Networks",           "forgenetworks.example"),
    ("Harbor & Main",            "harborandmain.example"),
    ("Verdant Software",         "verdant.example"),
    ("Lantern Systems",          "lanternsystems.example"),
    ("Cascade Biotech",          "cascadebio.example"),
    ("Pearl Street Games",       "pearlstreetgames.example"),
    ("Summit Logistics",         "summitlogistics.example"),
    ("Delta Parallel",           "deltaparallel.example"),
    ("Nimbus Compute",           "nimbuscompute.example"),
    ("Ironclad Research",        "ironcladresearch.example"),
    ("Keystone Security",        "keystonesec.example"),
    ("Polaris Robotics",         "polarisrobotics.example"),
    ("Outrigger Studios",        "outriggerstudios.example"),
]

ROLE_TITLES = [
    "Senior Software Engineer", "Full-Stack Engineer", "Backend Engineer",
    "Platform Engineer", "Staff Engineer", "Engineering Manager",
    "Site Reliability Engineer", "Data Engineer", "API Engineer",
    "Developer Advocate", "Infrastructure Engineer", "Applications Engineer",
    "Principal Engineer", "Engineering Lead", "Machine Learning Engineer",
    "Security Engineer", "Mobile Engineer", "DevOps Engineer",
]

# Each full description clears the 60-word STUB_MIN_WORDS threshold
# comfortably. Pool of five flavours so the feed doesn't read like a
# photocopy.
DESCRIPTIONS = [
    (
        "We're hiring a senior engineer to own a core service that touches "
        "every customer interaction. You'll design the public API, implement "
        "the services behind it, run it in production, and be on-call for "
        "what you ship. Stack is Python, PostgreSQL, Redis, and a thin React "
        "admin console. Our team is small, we ship daily, and we expect "
        "strong written communication because most decisions happen in "
        "docs, not meetings. Remote-first, US timezones. We cover healthcare "
        "in full and give real equity — not the performative kind."
    ),
    (
        "Join a mid-sized platform team that keeps the lights on for a "
        "multi-tenant SaaS serving 30k+ businesses. You'll spend most of "
        "your time in Django and Go, some time in Kubernetes, and occasional "
        "time in the PostgreSQL query planner when the slow-log lights up. "
        "We value people who can reduce operational pain as much as ship "
        "features — if you enjoy deleting code and untangling systems, "
        "you'll fit in. Hybrid role, three days in Austin. Comp is above "
        "market for the region."
    ),
    (
        "Full-stack role on a product squad of six. You'll build features "
        "end to end: React + TypeScript on the front, Django REST on the "
        "back, PostgreSQL with judicious use of views and window functions. "
        "We care a lot about test coverage, uptime, and keeping our "
        "CI green — flaky tests get fixed, not retried. The team runs "
        "without a dedicated PM; engineers talk directly to customers and "
        "own their roadmap. Fully remote within North America."
    ),
    (
        "Backend-focused position with real scope: you'll lead the rewrite "
        "of our billing subsystem, which currently handles ~$40M ARR and "
        "has accumulated seven years of edge cases. Experience with "
        "double-entry accounting concepts, Stripe webhooks, and "
        "reconciliation batch jobs is a big plus. You'll work closely with "
        "our finance lead and have latitude to redesign from first "
        "principles. New York or remote. Senior-only role, target comp "
        "350–420k total."
    ),
    (
        "SRE role on a 20-person infrastructure org. Primary responsibility "
        "is our multi-region PostgreSQL fleet and the services that "
        "depend on it. You'll own failover playbooks, drive p95 latency "
        "work, and help other teams ship observability that actually "
        "helps during incidents. Comfort with Terraform, AWS, and writing "
        "runbooks that humans can follow at 3am is assumed. Rotation is "
        "one week in five; we pay the rotation bonus whether or not you "
        "get paged."
    ),
]

# Deliberately thin stubs — sub-20 words — to land in the stub bucket
# even if someone later tunes the threshold tighter.
STUBS = [
    "Senior Engineer. Apply now.",
    "Hiring — frontend dev. Details inside.",
    "Backend role at growing startup.",
    "Platform engineer wanted.",
    "Remote-friendly. Competitive pay.",
    "Full-time engineering role.",
]


class Command(BaseCommand):
    help = "Seed the guest user with a rich application funnel for the public sankey"

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete existing demo posts (title prefix 'Demo:') first",
        )

    def handle(self, *args, **options):
        guest = User.objects.filter(username="guest").first()
        if guest is None:
            self.stderr.write(
                self.style.ERROR("guest user not found — run `seed_guest` first.")
            )
            return

        if options["reset"]:
            deleted, _ = JobPost.objects.filter(
                created_by=guest, title__startswith="Demo:"
            ).delete()
            self.stdout.write(f"Cleared {deleted} prior demo rows")

        rng = random.Random(42)
        now = timezone.now()

        status_cache: dict = {}

        def _status(name):
            if name not in status_cache:
                status_cache[name] = Status.objects.get_or_create(status=name)[0]
            return status_cache[name]

        companies = [Company.objects.get_or_create(name=n)[0] for n, _ in COMPANIES]
        company_domain_map = dict(COMPANIES)

        created_posts = 0
        created_apps = 0
        created_statuses = 0
        scored = 0

        for bucket, count in FUNNEL_SHAPE.items():
            for i in range(count):
                company = rng.choice(companies)
                title = f"Demo: {rng.choice(ROLE_TITLES)}"
                if bucket == "stub":
                    description = rng.choice(STUBS)
                else:
                    description = rng.choice(DESCRIPTIONS)

                # Link: mostly use per-company domain; occasionally use
                # one of the board hostnames so the sources stacked bar
                # shows greenhouse/lever/etc. as distinct rows.
                if rng.random() < 0.35:
                    host = rng.choice(HOSTNAMES)
                    link = f"https://{host}/jobs/{bucket}-{i}-{rng.randint(1000, 9999)}"
                else:
                    host = company_domain_map.get(company.name, "example.com")
                    link = f"https://careers.{host}/{bucket}-{i}"

                # Keep posted_date within the last 30 days for the
                # "recent activity" feel, except for ghosted where
                # we deliberately age the post out.
                if bucket == "ghosted":
                    posted_days_ago = rng.randint(35, 45)
                else:
                    posted_days_ago = rng.randint(1, 29)
                post = JobPost.objects.create(
                    title=title,
                    company=company,
                    description=description,
                    posted_date=(now - timedelta(days=posted_days_ago)).date(),
                    source=rng.choice(SOURCES),
                    link=link,
                    created_by=guest,
                )
                created_posts += 1

                # ~55% of non-stub posts are scored — gives both the
                # scored hub and the unscored hub meaningful volume.
                if bucket != "stub" and rng.random() < 0.55:
                    Score.objects.create(
                        job_post=post,
                        user=guest,
                        score=rng.randint(55, 96),
                        status="complete",
                    )
                    scored += 1

                if bucket in ("no_application", "stub"):
                    continue

                app = JobApplication.objects.create(
                    user=guest,
                    job_post=post,
                    company=company,
                )
                created_apps += 1

                for status_name, days_ago in PATHS[bucket]:
                    # Jitter logged_at so a single bucket doesn't look
                    # like it happened all on the same day.
                    jitter = rng.randint(-3, 3)
                    JobApplicationStatus.objects.create(
                        application=app,
                        status=_status(status_name),
                        logged_at=now - timedelta(days=max(0, days_ago + jitter)),
                    )
                    created_statuses += 1

        self.stdout.write(
            self.style.SUCCESS(
                "Seeded demo pipeline:\n"
                f"  posts:        {created_posts}\n"
                f"  applications: {created_apps}\n"
                f"  status rows:  {created_statuses}\n"
                f"  scored:       {scored}\n"
                "All attached to the guest user. Bust the public report cache "
                "with `flush_demo_report_cache` (or just wait 5 min)."
            )
        )
