"""
Management command: seed_guest

Creates a read-only guest user (Danny Noonan from Caddyshack, reimagined as a
software engineer) with realistic demo data so prospective users can try the app.

Idempotent — exits cleanly if the guest user already exists.
"""
import uuid
from datetime import date, datetime, timezone

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from job_hunting.models import (
    Answer,
    Company,
    CoverLetter,
    JobApplication,
    JobPost,
    Question,
    Resume,
    Score,
    Scrape,
    Summary,
)
from job_hunting.models.experience import Experience
from job_hunting.models.resume_experience import ResumeExperience
from job_hunting.models.profile import Profile

User = get_user_model()

CAREER_DATA_MARKDOWN = """\
# Danny Noonan — Full-Stack Software Engineer

## Summary
I'm a full-stack engineer with 5+ years building web applications and APIs.
I got my start caddying at Bushwood Country Club to put myself through school
(yes, really), which taught me more about reading complex situations and keeping
my cool under pressure than any classroom ever could. Now I bring that same
focus to shipping reliable software.

## Skills
- **Languages**: Python, JavaScript (ES2022+), TypeScript, SQL, Bash
- **Backend**: Django, Django REST Framework, FastAPI, PostgreSQL, Redis
- **Frontend**: React, Ember.js, Tailwind CSS
- **Tools**: Docker, GitHub Actions, AWS (EC2, RDS, S3), Datadog

## Work History

### Senior Software Engineer — Spackler Systems (2022–present)
Built and maintained the core job-scheduling API serving 40k daily requests.
Led migration from monolith to service-oriented architecture, cutting p95
latency from 800ms to 120ms. Mentored two junior engineers.

### Software Engineer — Czervik Industries (2020–2022)
Full-stack feature development on a React + Django SaaS product.
Owned the CSV import pipeline used by 600+ enterprise customers.
Reduced import errors by 73% by adding row-level validation and retry logic.

### Junior Developer — Bushwood Country Club Tech (2019–2020)
First engineering role. Maintained a legacy PHP admin panel, then helped
rewrite it in Django. Learned fast, shipped faster.

## Voice / Writing Style
I write like I talk — direct, no fluff. If something needs three sentences
I don't use six. I favour concrete results over vague claims ("cut latency
by 85%" beats "improved system performance significantly").

## Target Roles
Senior Full-Stack Engineer or Backend Engineer at a company where engineers
have real ownership and the culture doesn't take itself too seriously.
"""

COVER_LETTER_1 = """\
Dear Hiring Team,

When I read the job description for Senior Full-Stack Engineer at Bushwood
Country Club Tech, I felt like someone had written it about me — then I
remembered I used to work there mowing fairways between commits.

In all seriousness: I bring five years of full-stack experience, a genuine
fondness for Django and React, and a track record of shipping things that
actually work in production. At Spackler Systems I led an API migration that
cut our p95 latency from 800ms to 120ms without a single customer-facing
incident. At Czervik Industries I owned a CSV import pipeline touching 600+
enterprise customers and drove error rates down 73%.

What I'm looking for is a team that values ownership, moves at a reasonable
pace, and doesn't require three approval layers to merge a PR. Based on what
I've read, Bushwood sounds like that place.

I'd love to talk.

— Danny Noonan
"""

COVER_LETTER_2 = """\
Hi Spackler Systems Recruiting,

I'll keep this short because I know you're busy and I respect your time.

I have five years of Python and Django experience, strong PostgreSQL skills,
and I've worked at scale (40k+ daily API requests, multi-region deployments).
I'm currently at Spackler, which gives me some inside knowledge of your
codebase — and strong opinions about what to fix next.

I'm not looking to jump for money alone. I want a harder problem and more
ownership. If that's what this role offers, I'd like to talk.

— Danny
"""

SCORE_1_EXPLANATION = """\
Strong match overall. Python, Django, and PostgreSQL experience aligns directly
with the stated requirements. REST API design and Docker experience both present.

Gaps: Job description mentions Kubernetes experience preferred — Danny's
background is Docker Compose / ECS, not Kubernetes. Easily addressed but worth
acknowledging. TypeScript is listed as "nice to have"; Danny has it.

Recommendation: apply.
"""

SCORE_2_EXPLANATION = """\
Reasonable match. Full-stack experience and React skills align with the frontend
requirements. Django backend experience is a clear fit.

Gaps: Role heavily emphasizes mobile-first development (React Native) — Danny's
mobile experience is limited to responsive web. This is a meaningful gap for a
role described as "50% mobile."

Recommendation: apply, but address the mobile gap explicitly in the cover letter.
"""

SCORE_3_EXPLANATION = """\
Excellent match. Backend-focused role maps precisely to Danny's strongest skills:
Django, PostgreSQL, REST APIs, and service migration experience. The job's
emphasis on reducing technical debt is directly addressed by his Czervik and
Spackler work history.

No significant gaps identified.

Recommendation: strong apply.
"""

QUESTIONS = [
    {
        "content": "Tell us about a time you had to deliver under significant pressure.",
        "answer": (
            "At Spackler we had a critical scheduling bug discovered on a Friday afternoon "
            "before a long weekend. The system that routes 40k daily jobs had a race condition "
            "that was silently dropping about 2% of tasks — we caught it because a customer "
            "noticed. I stayed on, found the root cause (a missing database-level lock), wrote "
            "a fix with a test that reproduced the failure, deployed it to staging, and had it "
            "in production by 10pm. Zero customer impact. The pressure honestly helped — when "
            "the stakes are clear, decisions get easier."
        ),
    },
    {
        "content": "Describe a situation where you improved a system significantly. What did you change and why?",
        "answer": (
            "The CSV import pipeline at Czervik was notorious. Customers would upload a file, "
            "wait five minutes, and then get a vague 'import failed' email with no indication "
            "of what went wrong. Support tickets were constant. I refactored it completely: "
            "added row-level validation that returned structured errors per row, added a retry "
            "queue for transient failures, and built a status page customers could actually read. "
            "Error rates dropped 73%, support tickets for imports dropped about 80%, and "
            "customers started trusting the feature again. The lesson: error messages are a "
            "product feature, not an afterthought."
        ),
    },
    {
        "content": "How do you approach mentoring junior engineers?",
        "answer": (
            "I try to give juniors real work, not toy tasks. At Spackler I had two junior "
            "engineers who mostly got assigned bug fixes when I joined. I started pulling "
            "them into design discussions, having them write the first draft of implementation "
            "plans, and doing proper code reviews with explanations rather than just approvals. "
            "Both of them shipped significant features within six months. The key is trusting "
            "people enough to let them fail in low-stakes situations before the stakes get high."
        ),
    },
]

SUMMARY_CONTENT = """\
Experienced full-stack engineer with 5+ years in Python/Django and React, known
for improving system reliability and shipping clean, well-tested code. Looking
for a senior role with strong ownership and a no-nonsense engineering culture.
"""


class Command(BaseCommand):
    help = "Seed the database with a guest user (Danny Noonan) and realistic demo data"

    def handle(self, *args, **options):
        if User.objects.filter(username="guest").exists():
            self.stdout.write(self.style.WARNING("Guest user already exists — skipping."))
            return

        # ── User + Profile ──────────────────────────────────────────────────
        guest = User.objects.create_user(
            username="guest",
            email="danny.noonan@example.com",
            password=uuid.uuid4().hex,
            first_name="Danny",
            last_name="Noonan",
        )
        Profile.objects.create(user=guest, is_guest=True)
        self.stdout.write(f"Created guest user: {guest.username} (id={guest.id})")

        # ── Companies ───────────────────────────────────────────────────────
        bushwood, _ = Company.objects.get_or_create(name="Bushwood Country Club Tech")
        spackler, _ = Company.objects.get_or_create(name="Spackler Systems")
        czervik, _ = Company.objects.get_or_create(name="Czervik Industries")

        # ── Experiences ─────────────────────────────────────────────────────
        exp1 = Experience.objects.create(
            title="Senior Software Engineer",
            company=spackler,
            start_date=date(2022, 3, 1),
            end_date=None,
            summary="Led API migration; cut p95 latency from 800ms to 120ms.",
        )
        exp2 = Experience.objects.create(
            title="Software Engineer",
            company=czervik,
            start_date=date(2020, 6, 1),
            end_date=date(2022, 2, 28),
            summary="Owned CSV import pipeline; reduced error rates 73%.",
        )
        exp3 = Experience.objects.create(
            title="Junior Developer",
            company=bushwood,
            start_date=date(2019, 5, 1),
            end_date=date(2020, 5, 31),
            summary="Helped rewrite legacy PHP admin panel in Django.",
        )

        # ── Resume ──────────────────────────────────────────────────────────
        resume = Resume.objects.create(
            user=guest,
            title="Danny Noonan — Senior Full-Stack Engineer",
            name="Danny Noonan",
            favorite=True,
        )
        for order, exp in enumerate([exp1, exp2, exp3]):
            ResumeExperience.objects.create(resume=resume, experience=exp, order=order)

        # ── Job Posts ───────────────────────────────────────────────────────
        post1 = JobPost.objects.create(
            title="Senior Full-Stack Engineer",
            company=bushwood,
            description=(
                "We're looking for a Senior Full-Stack Engineer to join our core product "
                "team. You'll own features end-to-end from API design through React UI. "
                "Stack: Python/Django, PostgreSQL, React, Docker. Strong communication skills "
                "required — you'll work closely with product and design."
            ),
            location="Remote",
            remote=True,
            posted_date=date(2026, 3, 15),
            link="https://jobs.bushwoodtech.example/senior-fse-001",
            created_by=guest,
        )
        post2 = JobPost.objects.create(
            title="Backend Platform Lead",
            company=spackler,
            description=(
                "Spackler Systems is hiring a Backend Platform Lead to own our core "
                "scheduling infrastructure. Python, Django, PostgreSQL, Kubernetes. You'll "
                "lead a small team and be accountable for API reliability and performance."
            ),
            location="Austin, TX (Hybrid)",
            remote=False,
            posted_date=date(2026, 3, 20),
            link="https://jobs.spacklersystems.example/backend-lead-002",
            created_by=guest,
        )
        post3 = JobPost.objects.create(
            title="Full-Stack Engineer — Product Team",
            company=czervik,
            description=(
                "Join the Czervik Industries product team as a Full-Stack Engineer. "
                "You'll work across our SaaS platform: React frontend, Django backend, "
                "PostgreSQL. We care about code quality, test coverage, and shipping fast."
            ),
            location="New York, NY",
            remote=False,
            posted_date=date(2026, 3, 22),
            link="https://jobs.czervik.example/fse-product-003",
            created_by=guest,
        )
        post4 = JobPost.objects.create(
            title="Senior Django Engineer",
            company=bushwood,
            description=(
                "Pure backend role. You'll own our Django REST API, write complex "
                "PostgreSQL queries, and help reduce technical debt accumulated over "
                "5 years of rapid growth. Docker, CI/CD, some AWS."
            ),
            location="Remote",
            remote=True,
            posted_date=date(2026, 3, 28),
            link="https://jobs.bushwoodtech.example/django-senior-004",
            created_by=guest,
        )
        post5 = JobPost.objects.create(
            title="Software Engineer — Platform",
            company=spackler,
            description=(
                "Mid-to-senior level platform engineering role. Python, Django, "
                "PostgreSQL, Redis, Celery. You'll build the infrastructure that "
                "the product team builds on top of."
            ),
            location="Remote",
            remote=True,
            posted_date=date(2026, 4, 1),
            link="https://jobs.spacklersystems.example/platform-eng-005",
            created_by=guest,
        )

        # ── Scrape ──────────────────────────────────────────────────────────
        Scrape.objects.create(
            url="https://jobs.bushwoodtech.example/senior-fse-001",
            company=bushwood,
            job_post=post1,
            status="parsed",
            scraped_at=datetime(2026, 3, 15, 14, 22, tzinfo=timezone.utc),
            job_content=post1.description,
        )

        # ── Scores ──────────────────────────────────────────────────────────
        Score.objects.create(
            score=85,
            status="complete",
            explanation=SCORE_1_EXPLANATION,
            job_post=post1,
            resume=resume,
            user=guest,
        )
        Score.objects.create(
            score=72,
            status="complete",
            explanation=SCORE_2_EXPLANATION,
            job_post=post3,
            resume=resume,
            user=guest,
        )
        Score.objects.create(
            score=91,
            status="complete",
            explanation=SCORE_3_EXPLANATION,
            job_post=post4,
            resume=resume,
            user=guest,
        )

        # ── Cover Letters ───────────────────────────────────────────────────
        cl1 = CoverLetter.objects.create(
            content=COVER_LETTER_1,
            user=guest,
            job_post=post1,
            company=bushwood,
            resume=resume,
            favorite=True,
            status="complete",
        )
        CoverLetter.objects.create(
            content=COVER_LETTER_2,
            user=guest,
            job_post=post2,
            company=spackler,
            resume=resume,
            favorite=False,
            status="complete",
        )

        # ── Summary ─────────────────────────────────────────────────────────
        Summary.objects.create(
            content=SUMMARY_CONTENT,
            user=guest,
            job_post_id=post1.id,
            status="complete",
        )

        # ── Applications ────────────────────────────────────────────────────
        app1 = JobApplication.objects.create(
            user=guest,
            job_post=post1,
            company=bushwood,
            resume=resume,
            cover_letter=cl1,
            applied_at=datetime(2026, 3, 18, tzinfo=timezone.utc),
            status="phone_screen",
            notes="Good conversation with the recruiter. Technical screen scheduled for next week.",
        )
        JobApplication.objects.create(
            user=guest,
            job_post=post2,
            company=spackler,
            resume=resume,
            applied_at=datetime(2026, 3, 25, tzinfo=timezone.utc),
            status="submitted",
            notes="Applied via LinkedIn. Waiting to hear back.",
        )
        JobApplication.objects.create(
            user=guest,
            job_post=post4,
            company=bushwood,
            resume=resume,
            status="draft",
            notes="Want to tailor the cover letter before applying.",
        )

        # ── Questions + Answers ─────────────────────────────────────────────
        for q_data in QUESTIONS:
            q = Question.objects.create(
                content=q_data["content"],
                application=app1,
                company=bushwood,
                created_by=guest,
            )
            Answer.objects.create(
                question=q,
                content=q_data["answer"],
                favorite=True,
                status="complete",
            )

        self.stdout.write(self.style.SUCCESS(
            "Demo data seeded successfully.\n"
            "  Username: guest\n"
            "  Access:   read-only (no login credentials needed — use 'Try Demo' button)"
        ))
