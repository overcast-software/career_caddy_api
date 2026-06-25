"""CC-77 #79 — Score integer PK -> 10-char NanoID PK (true PK swap).

These tests run against the post-migration schema: the test DB is built by
applying every migration, including ``0116_score_nanoid_pk_swap``. A broken
forward swap fails the whole suite at DB-build time, so importing + querying
Score is itself a smoke test of the migration.

Score is a leaf (nothing FKs to it), so beyond the NanoIDModel contract we
assert that its outbound relations still resolve and that the two composite
UNIQUE constraints — which ride on ``job_post_id`` / ``resume_id`` /
``user_id``, never on ``score.id`` — survive the PK swap unchanged.
"""

import re

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from job_hunting.models import (
    NANOID_REGEX,
    NANOID_SIZE,
    JobPost,
    NanoIDModel,
    Resume,
    Score,
)

User = get_user_model()


class ScoreNanoIdPkContractTests(TestCase):
    def test_score_is_a_nanoidmodel(self):
        self.assertTrue(issubclass(Score, NanoIDModel))
        pk_field = Score._meta.pk
        self.assertEqual(pk_field.name, "id")
        self.assertTrue(pk_field.primary_key)
        self.assertEqual(pk_field.max_length, NANOID_SIZE)
        self.assertFalse(pk_field.editable)

    def test_created_score_gets_nanoid_pk(self):
        s = Score.objects.create(score=7)
        self.assertIsInstance(s.pk, str)
        self.assertTrue(re.fullmatch(NANOID_REGEX, s.pk), s.pk)
        self.assertEqual(Score.objects.get(pk=s.pk).score, 7)

    def test_distinct_pks(self):
        a = Score.objects.create(score=1)
        b = Score.objects.create(score=2)
        self.assertNotEqual(a.pk, b.pk)


class ScoreOutboundRelationsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="cc79score", email="s@example.com")
        self.jp = JobPost.objects.create(title="Backend Engineer")

    def test_job_post_relation_round_trips(self):
        s = Score.objects.create(job_post=self.jp, user=self.user, score=8)
        s.refresh_from_db()
        self.assertEqual(s.job_post_id, self.jp.pk)
        self.assertEqual(list(self.jp.scores.all()), [s])


class ScoreUniqueConstraintTests(TestCase):
    """The composite UNIQUE constraints must still be enforced after the swap."""

    def setUp(self):
        self.user = User.objects.create(username="cc79scoreu", email="u@example.com")
        self.jp = JobPost.objects.create(title="SRE")

    def test_unique_score_per_job_resume_user(self):
        resume = Resume.objects.create(favorite=False)
        Score.objects.create(job_post=self.jp, resume=resume, user=self.user, score=5)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Score.objects.create(
                    job_post=self.jp, resume=resume, user=self.user, score=6
                )

    def test_unique_score_per_job_user_career_data(self):
        Score.objects.create(job_post=self.jp, resume=None, user=self.user, score=5)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Score.objects.create(
                    job_post=self.jp, resume=None, user=self.user, score=6
                )
