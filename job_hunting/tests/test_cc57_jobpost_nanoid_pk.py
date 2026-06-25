"""CC-57 — JobPost integer PK -> 10-char NanoID PK (true PK swap).

These tests run against the post-migration schema: the test DB is built by
applying every migration, including ``0115_jobpost_nanoid_pk_swap``. A
broken forward swap fails the whole suite at DB-build time, so simply
importing + querying these models is itself a smoke test of the migration.

On top of that we assert:
  * the NanoIDModel contract on JobPost's PK,
  * that every one of the 13 FKs that reference ``job_post(id)`` — the 11
    dependent FKs plus the two self-FKs ``duplicate_of`` / ``reposted_from``
    — round-trips with the NanoID value and traverses both ways, and
  * that the composite UNIQUE constraints that ride on a job_post FK column
    (score x2, job_post_discovery) are still enforced after the swap.

The reverse (down) path is exercised separately on a throwaway scratch DB
(see the CC-57 handoff) — Django's test DB only migrates forward.
"""

import re

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from job_hunting.models import (
    NANOID_REGEX,
    NANOID_SIZE,
    CoverLetter,
    DuplicateAnnotation,
    JobApplication,
    JobPost,
    JobPostDescriptionDecision,
    JobPostDiscovery,
    JobPostOverwriteDecision,
    NanoIDModel,
    Question,
    Resume,
    Score,
    Scrape,
)

User = get_user_model()


class JobPostNanoIdPkContractTests(TestCase):
    def test_jobpost_is_a_nanoidmodel(self):
        self.assertTrue(issubclass(JobPost, NanoIDModel))
        pk_field = JobPost._meta.pk
        self.assertEqual(pk_field.name, "id")
        self.assertTrue(pk_field.primary_key)
        self.assertEqual(pk_field.max_length, NANOID_SIZE)
        self.assertFalse(pk_field.editable)

    def test_created_jobpost_gets_nanoid_pk(self):
        jp = JobPost.objects.create(title="Staff Engineer")
        self.assertIsInstance(jp.pk, str)
        self.assertTrue(re.fullmatch(NANOID_REGEX, jp.pk), jp.pk)
        # Round-trips through the DB as the same opaque string id.
        self.assertEqual(JobPost.objects.get(pk=jp.pk).title, "Staff Engineer")

    def test_distinct_pks(self):
        a = JobPost.objects.create(title="A")
        b = JobPost.objects.create(title="B")
        self.assertNotEqual(a.pk, b.pk)


class JobPostSelfForeignKeyTests(TestCase):
    """The two self-FKs are the trickiest part of the swap — they live on
    the parent table and reference it."""

    def test_duplicate_of_round_trips(self):
        canonical = JobPost.objects.create(title="Canonical")
        dupe = JobPost.objects.create(title="Dupe", duplicate_of=canonical)
        dupe.refresh_from_db()
        self.assertEqual(dupe.duplicate_of_id, canonical.pk)
        self.assertIsInstance(dupe.duplicate_of_id, str)
        self.assertEqual(list(canonical.duplicates.all()), [dupe])
        # The .canonical chain-walk still resolves.
        self.assertEqual(dupe.canonical, canonical)

    def test_reposted_from_round_trips(self):
        original = JobPost.objects.create(title="Original listing")
        repost = JobPost.objects.create(title="Re-listing", reposted_from=original)
        repost.refresh_from_db()
        self.assertEqual(repost.reposted_from_id, original.pk)
        self.assertEqual(list(original.reposts.all()), [repost])


class JobPostDependentForeignKeyTests(TestCase):
    """One round-trip per dependent FK column, proving each repointed
    correctly to the NanoID PK."""

    def setUp(self):
        self.user = User.objects.create(username="cc57", email="cc57@example.com")
        self.jp = JobPost.objects.create(title="Backend Engineer")

    def _assert_fk(self, obj, attr):
        obj.refresh_from_db()
        self.assertEqual(getattr(obj, f"{attr}_id"), self.jp.pk)
        self.assertIsInstance(getattr(obj, f"{attr}_id"), str)

    def test_score_fk(self):
        s = Score.objects.create(job_post=self.jp, user=self.user, score=7)
        self._assert_fk(s, "job_post")
        self.assertEqual(list(self.jp.scores.all()), [s])

    def test_job_application_fk(self):
        app = JobApplication.objects.create(job_post=self.jp, user=self.user)
        self._assert_fk(app, "job_post")
        self.assertEqual(list(self.jp.applications.all()), [app])

    def test_scrape_fk(self):
        sc = Scrape.objects.create(job_post=self.jp, url="https://x/y")
        self._assert_fk(sc, "job_post")
        self.assertEqual(list(self.jp.scrapes.all()), [sc])

    def test_cover_letter_fk(self):
        cl = CoverLetter.objects.create(job_post=self.jp, user=self.user)
        self._assert_fk(cl, "job_post")
        self.assertEqual(list(self.jp.cover_letters.all()), [cl])

    def test_question_fk(self):
        q = Question.objects.create(job_post=self.jp, created_by=self.user)
        self._assert_fk(q, "job_post")
        self.assertEqual(list(self.jp.direct_questions.all()), [q])

    def test_overwrite_decision_fk(self):
        d = JobPostOverwriteDecision.objects.create(job_post=self.jp)
        self._assert_fk(d, "job_post")
        self.assertEqual(list(self.jp.overwrite_decisions.all()), [d])

    def test_description_decision_fk(self):
        d = JobPostDescriptionDecision.objects.create(
            job_post=self.jp,
            existing_description_hash="a" * 8,
            new_description_hash="b" * 8,
            existing_word_count=10,
            new_word_count=20,
            choice="use_new",
            confidence="high",
        )
        self._assert_fk(d, "job_post")
        self.assertEqual(list(self.jp.description_decisions.all()), [d])

    def test_discovery_fk(self):
        disc = JobPostDiscovery.objects.create(job_post=self.jp, user=self.user)
        self._assert_fk(disc, "job_post")
        self.assertEqual(list(self.jp.discoveries.all()), [disc])

    def test_duplicate_annotation_fks(self):
        other = JobPost.objects.create(title="Other")
        ann = DuplicateAnnotation.objects.create(
            from_jp=self.jp,
            to_jp=other,
            previous_to=other,
            action=DuplicateAnnotation.MARK,
            set_by=self.user,
        )
        ann.refresh_from_db()
        self.assertEqual(ann.from_jp_id, self.jp.pk)
        self.assertEqual(ann.to_jp_id, other.pk)
        self.assertEqual(ann.previous_to_id, other.pk)
        self.assertEqual(list(self.jp.duplicate_annotations.all()), [ann])


class JobPostFkUniqueConstraintTests(TestCase):
    """The composite UNIQUE constraints that include a job_post FK column
    must survive the swap rebuilt on the NanoID values."""

    def setUp(self):
        self.user = User.objects.create(username="cc57u", email="u@example.com")
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
        # The partial UNIQUE (job_post, user) WHERE resume IS NULL.
        Score.objects.create(job_post=self.jp, resume=None, user=self.user, score=5)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Score.objects.create(
                    job_post=self.jp, resume=None, user=self.user, score=6
                )

    def test_unique_discovery_per_user_post(self):
        JobPostDiscovery.objects.create(job_post=self.jp, user=self.user)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                JobPostDiscovery.objects.create(job_post=self.jp, user=self.user)
