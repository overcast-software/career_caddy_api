"""CC-77 #79 — Question integer PK -> 10-char NanoID PK (true PK swap).

Beyond the NanoIDModel contract we assert the one FK that references
``question(id)`` — ``answer.question_id`` (CASCADE, NOT NULL) — round-trips
with the NanoID value and traverses both ways, and that the CASCADE delete
still fires.
"""

import re

from django.test import TestCase

from job_hunting.models import (
    NANOID_REGEX,
    NANOID_SIZE,
    Answer,
    NanoIDModel,
    Question,
)


class QuestionNanoIdPkContractTests(TestCase):
    def test_question_is_a_nanoidmodel(self):
        self.assertTrue(issubclass(Question, NanoIDModel))
        pk_field = Question._meta.pk
        self.assertEqual(pk_field.name, "id")
        self.assertTrue(pk_field.primary_key)
        self.assertEqual(pk_field.max_length, NANOID_SIZE)
        self.assertFalse(pk_field.editable)

    def test_created_question_gets_nanoid_pk(self):
        q = Question.objects.create(content="Why this role?")
        self.assertIsInstance(q.pk, str)
        self.assertTrue(re.fullmatch(NANOID_REGEX, q.pk), q.pk)
        self.assertEqual(Question.objects.get(pk=q.pk).content, "Why this role?")

    def test_distinct_pks(self):
        a = Question.objects.create()
        b = Question.objects.create()
        self.assertNotEqual(a.pk, b.pk)


class QuestionDependentForeignKeyTests(TestCase):
    def test_answer_question_fk_round_trips(self):
        q = Question.objects.create()
        ans = Answer.objects.create(question=q, content="An answer")
        ans.refresh_from_db()
        self.assertEqual(ans.question_id, q.pk)
        self.assertIsInstance(ans.question_id, str)
        self.assertEqual(list(q.answers.all()), [ans])

    def test_cascade_delete_removes_answers(self):
        q = Question.objects.create()
        Answer.objects.create(question=q)
        Answer.objects.create(question=q)
        self.assertEqual(Answer.objects.count(), 2)
        q.delete()
        self.assertEqual(Answer.objects.count(), 0)
