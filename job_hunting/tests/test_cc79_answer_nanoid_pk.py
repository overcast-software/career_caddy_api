"""CC-77 #79 — Answer integer PK -> 10-char NanoID PK (true PK swap).

Answer is a leaf (nothing FKs to it). Beyond the NanoIDModel contract we
assert its outbound ``question`` FK still resolves both ways.
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


class AnswerNanoIdPkContractTests(TestCase):
    def test_answer_is_a_nanoidmodel(self):
        self.assertTrue(issubclass(Answer, NanoIDModel))
        pk_field = Answer._meta.pk
        self.assertEqual(pk_field.name, "id")
        self.assertTrue(pk_field.primary_key)
        self.assertEqual(pk_field.max_length, NANOID_SIZE)
        self.assertFalse(pk_field.editable)

    def test_created_answer_gets_nanoid_pk(self):
        q = Question.objects.create()
        a = Answer.objects.create(question=q, content="Because.")
        self.assertIsInstance(a.pk, str)
        self.assertTrue(re.fullmatch(NANOID_REGEX, a.pk), a.pk)
        self.assertEqual(Answer.objects.get(pk=a.pk).content, "Because.")

    def test_distinct_pks(self):
        q = Question.objects.create()
        a = Answer.objects.create(question=q)
        b = Answer.objects.create(question=q)
        self.assertNotEqual(a.pk, b.pk)

    def test_question_relation_round_trips(self):
        q = Question.objects.create()
        a = Answer.objects.create(question=q)
        a.refresh_from_db()
        self.assertEqual(a.question_id, q.pk)
        self.assertEqual(list(q.answers.all()), [a])
