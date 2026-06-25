"""CC-77 #79 — JobApplication integer PK -> 10-char NanoID PK (true PK swap).

Beyond the NanoIDModel contract we assert both FKs that reference
``job_application(id)`` round-trip with the NanoID value and traverse both
ways:

    job_application_status.application_id   (CASCADE,  NOT NULL)
    question.application_id                 (SET_NULL, nullable)
"""

import re

from django.test import TestCase

from job_hunting.models import (
    NANOID_REGEX,
    NANOID_SIZE,
    JobApplication,
    JobApplicationStatus,
    NanoIDModel,
    Question,
)


class JobApplicationNanoIdPkContractTests(TestCase):
    def test_jobapplication_is_a_nanoidmodel(self):
        self.assertTrue(issubclass(JobApplication, NanoIDModel))
        pk_field = JobApplication._meta.pk
        self.assertEqual(pk_field.name, "id")
        self.assertTrue(pk_field.primary_key)
        self.assertEqual(pk_field.max_length, NANOID_SIZE)
        self.assertFalse(pk_field.editable)

    def test_created_jobapplication_gets_nanoid_pk(self):
        app = JobApplication.objects.create(status="Applied")
        self.assertIsInstance(app.pk, str)
        self.assertTrue(re.fullmatch(NANOID_REGEX, app.pk), app.pk)
        self.assertEqual(JobApplication.objects.get(pk=app.pk).status, "Applied")

    def test_distinct_pks(self):
        a = JobApplication.objects.create()
        b = JobApplication.objects.create()
        self.assertNotEqual(a.pk, b.pk)


class JobApplicationDependentForeignKeyTests(TestCase):
    def test_status_application_fk_round_trips(self):
        app = JobApplication.objects.create()
        st = JobApplicationStatus.objects.create(application=app)
        st.refresh_from_db()
        self.assertEqual(st.application_id, app.pk)
        self.assertIsInstance(st.application_id, str)
        self.assertEqual(list(app.application_statuses.all()), [st])

    def test_question_application_fk_round_trips(self):
        app = JobApplication.objects.create()
        q = Question.objects.create(application=app)
        q.refresh_from_db()
        self.assertEqual(q.application_id, app.pk)
        self.assertIsInstance(q.application_id, str)
        self.assertEqual(list(app.questions.all()), [q])

    def test_cascade_delete_removes_statuses(self):
        app = JobApplication.objects.create()
        JobApplicationStatus.objects.create(application=app)
        self.assertEqual(JobApplicationStatus.objects.count(), 1)
        app.delete()
        self.assertEqual(JobApplicationStatus.objects.count(), 0)

    def test_set_null_on_question_when_application_deleted(self):
        app = JobApplication.objects.create()
        q = Question.objects.create(application=app)
        app.delete()
        q.refresh_from_db()
        self.assertIsNone(q.application_id)
