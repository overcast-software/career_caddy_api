"""BACK-97 (Task A) — AP Note content builder: lean default + rich opt-in.

Pins the "actively hiring" regression and the locked design choices:
lean line composer (null-safe, thin-stub hook drop), rich verdict line
(reason_code not free-text note, score bucket + raw), Person→rich vs
Company→lean gating (no score leak), url precedence (resolved apply_url →
canonical_link → link, internal /job-posts/<pk> floor dropped), no AS2
``summary`` (Mastodon CW trap), and the ≤500-char budget shrinking the
hook first.
"""
from __future__ import annotations

import html as _html
import re

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from job_hunting.lib.as_object import (
    AS2_PUBLIC,
    build_create_activity_for_jobpost,
    build_jobpost_note,
    build_note_object_for_jobpost,
)
from job_hunting.models import (
    Actor,
    Company,
    JobApplication,
    JobApplicationStatus,
    JobPost,
    Profile,
    Score,
    Status,
)

User = get_user_model()

_TAG_RE = re.compile(r"<[^>]+>")


def _visible(content: str, url: str | None = None) -> str:
    """Approximate Mastodon's visible char accounting: de-tag, newline the
    breaks, count a URL as a flat 23 chars."""
    text = content.replace("<br>", "\n")
    text = _TAG_RE.sub("", text)
    text = _html.unescape(text)
    if url and url in text:
        text = text.replace(url, "x" * 23)
    return text


def _content(note: dict) -> str:
    return note.get("content") or ""


@override_settings(INSTANCE_ORIGIN="http://testserver", CAREER_CADDY_INSTANCE="testserver")
class TestLeanBuilder(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="lean", password="p")

    def _post(self, **kw):
        defaults = dict(
            created_by=self.user,
            title="Senior Platform Engineer",
            description="We build resilient distributed systems for robots.",
            location="Remote (US)",
            remote=True,
            salary_min=150000,
            salary_max=185000,
            link="https://acme.example/careers/123",
            complete=True,
            audience=[AS2_PUBLIC],
        )
        defaults.update(kw)
        company = Company.objects.create(name=kw.pop("company_name", "Acme Robotics"))
        defaults["company"] = company
        return JobPost.objects.create(**defaults)

    def test_lean_lines_present_and_null_safe(self):
        post = self._post()
        note = build_jobpost_note(post, "http://testserver/actors/lean", rich=False)
        content = _content(note)
        self.assertIn("🟢 Senior Platform Engineer — Acme Robotics", content)
        self.assertIn("📍 Remote (US)", content)
        self.assertIn("💰 $150k–$185k", content)
        self.assertIn("We build resilient", content)  # the real hook
        self.assertIn("#hiring", content)
        self.assertIn("#remotejobs", content)
        self.assertIn("#platformengineer", content)
        # Null-safety: never the literal word None anywhere.
        self.assertNotIn("None", content)

    def test_thin_stub_drops_hook_kills_actively_hiring(self):
        # The pinned regression: a thin LLM stub (complete=False) whose
        # description IS the junk sentence must NOT echo it.
        post = self._post(
            description="This company is actively hiring, based in Seattle, WA.",
            complete=False,
        )
        note = build_jobpost_note(post, "http://testserver/actors/lean", rich=False)
        content = _content(note)
        self.assertNotIn("actively hiring", content)
        # but the actionable scaffold survives
        self.assertIn("Senior Platform Engineer", content)
        self.assertIn("Acme Robotics", content)
        self.assertIn("acme.example/careers/123", content)

    def test_only_title_no_none_artifacts(self):
        user = User.objects.create_user(username="sparse", password="p")
        post = JobPost.objects.create(
            created_by=user,
            title="Data Scientist",
            link="https://x.example/j/1",
            audience=[AS2_PUBLIC],
        )
        note = build_jobpost_note(post, "http://testserver/actors/sparse", rich=False)
        content = _content(note)
        self.assertIn("🟢 Data Scientist", content)
        self.assertNotIn("None", content)
        self.assertNotIn("📍", content)  # no location / not remote → line dropped
        self.assertNotIn("💰", content)  # no salary → line dropped

    def test_never_emits_summary(self):
        post = self._post()
        note = build_jobpost_note(post, "http://testserver/actors/lean", rich=True)
        self.assertNotIn("summary", note)

    def test_budget_shrinks_hook_first(self):
        long_desc = "Lorem ipsum dolor sit amet. " * 60  # ~1680 chars
        post = self._post(description=long_desc)
        note = build_jobpost_note(post, "http://testserver/actors/lean", rich=False)
        content = _content(note)
        self.assertIn("…", content)  # hook truncated
        visible = _visible(content, url="https://acme.example/careers/123")
        # newlines count too; allow the line-break chars
        self.assertLessEqual(len(visible), 500)

    def test_url_precedence_resolved_apply_url_wins(self):
        post = self._post(
            apply_url="https://boards.example/apply/9",
            apply_url_status="resolved",
            canonical_link="https://acme.example/careers/123?clean",
        )
        note = build_jobpost_note(post, "http://testserver/actors/lean")
        self.assertEqual(note["url"], "https://boards.example/apply/9")

    def test_url_precedence_unresolved_apply_url_skipped(self):
        post = self._post(
            apply_url="https://boards.example/apply/9",
            apply_url_status="unknown",
            canonical_link="https://acme.example/canon",
        )
        note = build_jobpost_note(post, "http://testserver/actors/lean")
        self.assertEqual(note["url"], "https://acme.example/canon")

    def test_url_precedence_falls_to_link(self):
        post = self._post(apply_url=None, canonical_link=None)
        note = build_jobpost_note(post, "http://testserver/actors/lean")
        self.assertEqual(note["url"], "https://acme.example/careers/123")

    def test_object_id_is_machine_uri_not_dropped(self):
        post = self._post()
        note = build_jobpost_note(post, "http://testserver/actors/lean")
        self.assertEqual(note["id"], f"http://testserver/job-posts/{post.pk}")


@override_settings(INSTANCE_ORIGIN="http://testserver", CAREER_CADDY_INSTANCE="testserver")
class TestRichGatingAndVerdict(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="dough", password="p")
        Profile.objects.create(user=self.owner, federate_rich=True)
        self.company = Company.objects.create(name="Acme Robotics")
        self.post = JobPost.objects.create(
            created_by=self.owner,
            title="Senior Platform Engineer",
            description="We build resilient distributed systems for robots.",
            location="Remote (US)",
            remote=True,
            salary_min=150000,
            salary_max=185000,
            link="https://acme.example/careers/123",
            complete=True,
            company=self.company,
            audience=[AS2_PUBLIC],
        )
        self.person_actor = Actor.objects.create(
            user=self.owner, type="Person", preferred_username="dough"
        )

    def _vet(self, name, reason_code=None):
        app = JobApplication.objects.create(job_post=self.post, user=self.owner)
        status = Status.objects.get_or_create(status=name)[0]
        from django.utils import timezone
        JobApplicationStatus.objects.create(
            application=app, status=status, logged_at=timezone.now(),
            reason_code=reason_code,
        )
        return app

    def _score(self, value):
        Score.objects.create(job_post=self.post, user=self.owner, score=value)

    def test_person_owner_optedin_renders_rich(self):
        self._vet("Vetted Good")
        self._score(87)
        app = JobApplication.objects.filter(job_post=self.post, user=self.owner).first()
        from django.utils import timezone
        app.applied_at = timezone.now()
        app.save(update_fields=["applied_at"])
        activity = build_create_activity_for_jobpost(self.post, self.person_actor)
        content = activity["object"]["content"]
        self.assertIn("✅ Vetted good", content)
        self.assertIn("Strong match (87)", content)
        self.assertIn("applied", content)

    def test_vetted_bad_shows_reason_code_not_note(self):
        app = JobApplication.objects.create(job_post=self.post, user=self.owner)
        status = Status.objects.get_or_create(status="Vetted Bad")[0]
        from django.utils import timezone
        JobApplicationStatus.objects.create(
            application=app, status=status, logged_at=timezone.now(),
            reason_code="compensation", note="pays peanuts secretly",
        )
        activity = build_create_activity_for_jobpost(self.post, self.person_actor)
        content = activity["object"]["content"]
        self.assertIn("❌ Vetted bad (compensation)", content)
        self.assertNotIn("peanuts", content)

    def test_score_buckets(self):
        from job_hunting.lib.as_object import _score_segment
        self.assertEqual(_score_segment(87), "Strong match (87)")
        self.assertEqual(_score_segment(72), "Good match (72)")
        self.assertEqual(_score_segment(40), "Weak match (40)")
        self.assertIsNone(_score_segment(None))

    def test_company_actor_always_lean_no_score_leak(self):
        self._vet("Vetted Good")
        self._score(91)
        self.company.slug = "acme-robotics"
        self.company.save(update_fields=["slug"])
        org_actor = Actor.objects.create(
            company=self.company, type="Organization",
            preferred_username="acme-robotics",
        )
        activity = build_create_activity_for_jobpost(self.post, org_actor)
        content = activity["object"]["content"]
        self.assertNotIn("Vetted", content)
        self.assertNotIn("match (91)", content)

    def test_non_optedin_owner_renders_lean(self):
        plain = User.objects.create_user(username="plain", password="p")
        Profile.objects.create(user=plain, federate_rich=False)
        post = JobPost.objects.create(
            created_by=plain, title="QA", description="d",
            link="https://x.example/qa", complete=True, audience=[AS2_PUBLIC],
        )
        Score.objects.create(job_post=post, user=plain, score=88)
        actor = Actor.objects.create(
            user=plain, type="Person", preferred_username="plain"
        )
        activity = build_create_activity_for_jobpost(post, actor)
        self.assertNotIn("match (88)", activity["object"]["content"])

    def test_standalone_fetch_owner_actor_is_rich(self):
        self._vet("Vetted Good")
        self._score(81)
        note = build_note_object_for_jobpost(self.post, self.person_actor)
        self.assertIn("Strong match (81)", note["content"])
        self.assertEqual(note["@context"], "https://www.w3.org/ns/activitystreams")

    def test_standalone_fetch_no_actor_is_lean(self):
        self._vet("Vetted Good")
        self._score(81)
        note = build_note_object_for_jobpost(self.post, None)
        self.assertNotIn("Vetted", note["content"])
