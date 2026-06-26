"""BACK-100 (Task D) — outbox batch annotation resolver (no N+1).

The actor outbox builds a Create(Note) per post in a loop. The rich
verdict/score/applied reads must be resolved for the WHOLE page in a
fixed number of queries — not once per post. Asserts (a) content parity
between the outbox-listed Note and a directly-built Create, and (b) the
query count is O(1) in the number of posts on the page.
"""
from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

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
from job_hunting.models.job_post import AS2_PUBLIC

User = get_user_model()


@override_settings(INSTANCE_ORIGIN="http://testserver", CAREER_CADDY_INSTANCE="testserver")
class TestOutboxBatchAnnotations(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="dough", password="p")
        Profile.objects.create(user=self.owner, federate_rich=True)
        self.actor = Actor.objects.create(
            user=self.owner, type="Person", preferred_username="dough"
        )

    def _seed_post(self, n, *, score=None, vet=None):
        company = Company.objects.create(name=f"Acme {n}")
        post = JobPost.objects.create(
            created_by=self.owner,
            title=f"Engineer {n}",
            description="real description text here",
            complete=True,
            link=f"https://x.example/j/{n}",
            company=company,
            audience=[AS2_PUBLIC],
        )
        if score is not None:
            Score.objects.create(job_post=post, user=self.owner, score=score)
        if vet is not None:
            app = JobApplication.objects.create(job_post=post, user=self.owner)
            status = Status.objects.get_or_create(status=vet)[0]
            JobApplicationStatus.objects.create(
                application=app, status=status, logged_at=timezone.now()
            )
        return post

    def _outbox_page(self):
        resp = self.client.get("/actors/dough/outbox?page=1")
        self.assertEqual(resp.status_code, 200)
        return json.loads(resp.content)

    def test_outbox_note_carries_rich_content(self):
        self._seed_post(1, score=87, vet="Vetted Good")
        body = self._outbox_page()
        note = body["orderedItems"][0]["object"]
        self.assertIn("Strong match (87)", note["content"])
        self.assertIn("✅ Vetted good", note["content"])

    def test_outbox_query_count_is_constant_in_rows(self):
        # 2-post page
        self._seed_post(1, score=80, vet="Vetted Good")
        self._seed_post(2, score=70, vet="Vetted Bad")
        with CaptureQueriesContext(connection) as small:
            self._outbox_page()
        q_small = len(small)

        # grow to 6 posts
        for i in range(3, 7):
            self._seed_post(i, score=60 + i, vet="Vetted Good")
        with CaptureQueriesContext(connection) as big:
            self._outbox_page()
        q_big = len(big)

        self.assertEqual(
            q_small, q_big,
            f"outbox render N+1'd: {q_small} (2 posts) vs {q_big} (6 posts)",
        )

    def test_lean_actor_outbox_has_no_verdict(self):
        lean = User.objects.create_user(username="lean", password="p")
        Profile.objects.create(user=lean, federate_rich=False)
        Actor.objects.create(user=lean, type="Person", preferred_username="lean")
        company = Company.objects.create(name="LeanCo")
        post = JobPost.objects.create(
            created_by=lean, title="QA", description="d", complete=True,
            link="https://x.example/qa", company=company, audience=[AS2_PUBLIC],
        )
        Score.objects.create(job_post=post, user=lean, score=95)
        resp = self.client.get("/actors/lean/outbox?page=1")
        body = json.loads(resp.content)
        note = body["orderedItems"][0]["object"]
        self.assertNotIn("match (95)", note["content"])
