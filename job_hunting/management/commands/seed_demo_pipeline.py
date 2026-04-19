"""
Management command: seed_demo_pipeline

Seeds the *guest* user with a rich application funnel so the public
/reports/application-flow sankey has something eye-catching for
anonymous visitors. Creates ~50 JobPosts distributed across every
sankey bucket (applied/interview/offer/ghosted/rejected/withdrew/
accepted/declined/no_application/stub) with matching
JobApplicationStatus histories and a mix of scored/unscored posts.

Idempotent with `--reset`: clears prior demo data first. Without
`--reset` it appends, which is usually what you want on a fresh DB.
Requires the `guest` user to exist — run `seed_guest` first.
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
# pipeline rather than a sad flatline — plenty at the top, tapering
# through stages, a real offer/accepted path visible at the end.
FUNNEL_SHAPE = {
    "applied": 14,
    "interview": 8,
    "offer": 3,
    "accepted": 2,
    "declined": 1,
    "rejected": 5,
    "withdrew": 2,
    "ghosted": 4,
    "no_application": 10,
    "stub": 7,
}

# Paths through statuses for each terminal bucket. Mirrors the bucket
# mapping in job_hunting.lib.services.application_flow.BUCKETS — each
# status name must be one the sankey knows about. Dates are 'days ago'
# so the test's GHOST_AFTER_DAYS=30 derivation fires naturally for the
# ghosted entries (first event far in the past, no follow-up).
PATHS = {
    "applied":     [("Applied", 12)],
    "interview":   [("Applied", 25), ("Phone Screen", 18), ("Interview Scheduled", 8)],
    "offer":       [("Applied", 40), ("Phone Screen", 30), ("Interviewed", 18), ("Offer", 4)],
    "accepted":    [("Applied", 50), ("Phone Screen", 42), ("Interviewed", 28), ("Offer", 14), ("Accepted", 6)],
    "declined":    [("Applied", 55), ("Phone Screen", 45), ("Interviewed", 30), ("Offer", 15), ("Declined", 7)],
    "rejected":    [("Applied", 20), ("Rejected", 10)],
    "withdrew":    [("Applied", 22), ("Withdrawn", 12)],
    "ghosted":     [("Applied", 60)],  # no follow-up → sankey derives ghosted
}

SOURCES = ["email", "email", "email", "scrape", "paste", "manual"]

COMPANY_NAMES = [
    "Bluebird Labs", "Meridian Health", "Copper Canyon Robotics",
    "Northwind Analytics", "Sycamore AI", "Ridgeline Payments",
    "Turnstile Data", "Forge Networks", "Harbor & Main",
    "Verdant Software", "Lantern Systems", "Cascade Biotech",
    "Pearl Street Games", "Summit Logistics", "Delta Parallel",
]

ROLE_TITLES = [
    "Senior Software Engineer", "Full-Stack Engineer", "Backend Engineer",
    "Platform Engineer", "Staff Engineer", "Engineering Manager",
    "Site Reliability Engineer", "Data Engineer", "API Engineer",
    "Developer Advocate", "Infrastructure Engineer", "Applications Engineer",
]

FULL_DESCRIPTION = (
    "We're hiring a senior engineer to own a core part of our platform end "
    "to end. You'll design APIs, write the services behind them, deploy to "
    "production, and own the on-call rotation for what you ship. Day to day "
    "you'll work in Python and TypeScript, PostgreSQL, and AWS. We value "
    "shipping over perfection, strong written communication, and engineers "
    "who can see past their own code to the customer problem. Compensation "
    "is competitive, healthcare is fully covered, and the team is small "
    "enough that your opinions actually move things."
)

STUB_DESCRIPTION = "Senior Engineer role, apply here."


class Command(BaseCommand):
    help = "Seed the guest user with a rich application funnel for the public sankey"

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete existing demo-pipeline posts (titled with 'Demo:' prefix) first",
        )

    def handle(self, *args, **options):
        guest = User.objects.filter(username="guest").first()
        if guest is None:
            self.stderr.write(
                self.style.ERROR(
                    "guest user not found — run `seed_guest` first."
                )
            )
            return

        if options["reset"]:
            deleted, _ = JobPost.objects.filter(
                created_by=guest, title__startswith="Demo:"
            ).delete()
            self.stdout.write(f"Cleared {deleted} prior demo rows")

        rng = random.Random(42)
        now = timezone.now()

        # Pre-materialise statuses + companies
        status_cache = {}

        def _status(name):
            if name not in status_cache:
                status_cache[name] = Status.objects.get_or_create(status=name)[0]
            return status_cache[name]

        companies = [
            Company.objects.get_or_create(name=n)[0] for n in COMPANY_NAMES
        ]

        created_posts = 0
        created_apps = 0
        created_statuses = 0

        for bucket, count in FUNNEL_SHAPE.items():
            for i in range(count):
                company = rng.choice(companies)
                title = f"Demo: {rng.choice(ROLE_TITLES)}"
                description = (
                    STUB_DESCRIPTION if bucket == "stub" else FULL_DESCRIPTION
                )
                posted_days_ago = rng.randint(5, 80)
                post = JobPost.objects.create(
                    title=title,
                    company=company,
                    description=description,
                    posted_date=(now - timedelta(days=posted_days_ago)).date(),
                    source=rng.choice(SOURCES),
                    link=f"https://jobs.{company.name.lower().replace(' ', '')}.example/{bucket}-{i}",
                    created_by=guest,
                )
                created_posts += 1

                # About 40% of non-stub posts get a score, giving the
                # scored/unscored hub layer meaningful volume on both
                # sides.
                if bucket != "stub" and rng.random() < 0.4:
                    Score.objects.create(
                        job_post=post,
                        user=guest,
                        score=rng.randint(55, 95),
                        status="complete",
                    )

                if bucket in ("no_application", "stub"):
                    continue

                app = JobApplication.objects.create(
                    user=guest,
                    job_post=post,
                    company=company,
                )
                created_apps += 1

                for status_name, days_ago in PATHS[bucket]:
                    JobApplicationStatus.objects.create(
                        application=app,
                        status=_status(status_name),
                        logged_at=now - timedelta(days=days_ago),
                    )
                    created_statuses += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded demo pipeline: {created_posts} posts, "
                f"{created_apps} applications, {created_statuses} status rows "
                "(all attached to the guest user)."
            )
        )
