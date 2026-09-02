"""BACK-136 — non-staff tenancy joins in ``resume_parts._owned_qs``.

The experience / education / certification / description viewsets spelled
their reverse accessor as Django's *default* lowercase model name
(``resumeexperience__resume__user_id`` and friends). Every one of those join
models sets ``related_name``, which also sets ``related_query_name`` and
removes the default accessor, so the filter raised ``FieldError`` and the
endpoint returned 500 for every non-staff caller.

``_owned_qs`` short-circuits to ``.objects.all()`` when ``request.user`` is
staff, so a staff fixture passes vacuously against the broken code — which is
exactly why this shipped and survived. Every test below authenticates a
**non-staff** user on purpose, and asserts tenancy isolation as well as the
status code so the join is exercised rather than merely tolerated.

``_owned_qs`` backs ``list``, ``retrieve`` and ``destroy`` alike, so both the
collection and the detail route are covered per viewset.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from job_hunting.models import (
    Certification,
    Description,
    Education,
    Experience,
    ExperienceDescription,
    Project,
    ProjectDescription,
    Resume,
    ResumeCertification,
    ResumeEducation,
    ResumeExperience,
)

User = get_user_model()


class ResumePartsNonStaffOwnedQsTests(TestCase):
    """Each viewset's ``_owned_qs`` must resolve for a non-staff requester."""

    def setUp(self):
        self.owner = User.objects.create_user(username="back136-owner", password="p")
        self.stranger = User.objects.create_user(username="back136-other", password="p")
        # The whole defect hides behind the is_staff short circuit.
        self.assertFalse(self.owner.is_staff)

        self.resume = Resume.objects.create(user=self.owner, title="Mine")
        self.other_resume = Resume.objects.create(user=self.stranger, title="Theirs")

        self.experience = Experience.objects.create(title="Owned engineer")
        ResumeExperience.objects.create(resume=self.resume, experience=self.experience)
        self.other_experience = Experience.objects.create(title="Stranger engineer")
        ResumeExperience.objects.create(
            resume=self.other_resume, experience=self.other_experience
        )

        self.education = Education.objects.create(institution="Owned University")
        ResumeEducation.objects.create(resume=self.resume, education=self.education)
        self.other_education = Education.objects.create(institution="Stranger College")
        ResumeEducation.objects.create(
            resume=self.other_resume, education=self.other_education
        )

        self.certification = Certification.objects.create(title="Owned cert")
        ResumeCertification.objects.create(
            resume=self.resume, certification=self.certification
        )
        self.other_certification = Certification.objects.create(title="Stranger cert")
        ResumeCertification.objects.create(
            resume=self.other_resume, certification=self.other_certification
        )

        # Descriptions reach the owner down two independent legs.
        self.exp_description = Description.objects.create(content="via experience")
        ExperienceDescription.objects.create(
            experience=self.experience, description=self.exp_description
        )
        self.project = Project.objects.create(user=self.owner, title="Owned project")
        self.proj_description = Description.objects.create(content="via project")
        ProjectDescription.objects.create(
            project=self.project, description=self.proj_description
        )

        self.other_exp_description = Description.objects.create(content="not yours")
        ExperienceDescription.objects.create(
            experience=self.other_experience, description=self.other_exp_description
        )
        self.other_project = Project.objects.create(
            user=self.stranger, title="Stranger project"
        )
        self.other_proj_description = Description.objects.create(content="nor this")
        ProjectDescription.objects.create(
            project=self.other_project, description=self.other_proj_description
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

    def _list_ids(self, path):
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        return {str(item["id"]) for item in resp.json()["data"]}

    # --- ExperienceViewSet ------------------------------------------------

    def test_experiences_list_non_staff(self):
        ids = self._list_ids("/api/v1/experiences/")
        self.assertIn(str(self.experience.id), ids)
        self.assertNotIn(str(self.other_experience.id), ids)

    def test_experiences_retrieve_non_staff(self):
        resp = self.client.get(f"/api/v1/experiences/{self.experience.id}/")
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        self.assertEqual(str(resp.json()["data"]["id"]), str(self.experience.id))

        denied = self.client.get(f"/api/v1/experiences/{self.other_experience.id}/")
        self.assertEqual(denied.status_code, 404, denied.content[:400])

    # --- EducationViewSet -------------------------------------------------

    def test_educations_list_non_staff(self):
        ids = self._list_ids("/api/v1/educations/")
        self.assertIn(str(self.education.id), ids)
        self.assertNotIn(str(self.other_education.id), ids)

    def test_educations_retrieve_non_staff(self):
        resp = self.client.get(f"/api/v1/educations/{self.education.id}/")
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        self.assertEqual(str(resp.json()["data"]["id"]), str(self.education.id))

        denied = self.client.get(f"/api/v1/educations/{self.other_education.id}/")
        self.assertEqual(denied.status_code, 404, denied.content[:400])

    # --- CertificationViewSet ---------------------------------------------

    def test_certifications_list_non_staff(self):
        ids = self._list_ids("/api/v1/certifications/")
        self.assertIn(str(self.certification.id), ids)
        self.assertNotIn(str(self.other_certification.id), ids)

    def test_certifications_retrieve_non_staff(self):
        resp = self.client.get(f"/api/v1/certifications/{self.certification.id}/")
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        self.assertEqual(str(resp.json()["data"]["id"]), str(self.certification.id))

        denied = self.client.get(
            f"/api/v1/certifications/{self.other_certification.id}/"
        )
        self.assertEqual(denied.status_code, 404, denied.content[:400])

    # --- DescriptionViewSet — both legs of the union ----------------------

    def test_descriptions_list_non_staff_covers_both_legs(self):
        ids = self._list_ids("/api/v1/descriptions/")
        self.assertIn(str(self.exp_description.id), ids)
        self.assertIn(str(self.proj_description.id), ids)
        self.assertNotIn(str(self.other_exp_description.id), ids)
        self.assertNotIn(str(self.other_proj_description.id), ids)

    def test_descriptions_retrieve_non_staff(self):
        for owned in (self.exp_description, self.proj_description):
            resp = self.client.get(f"/api/v1/descriptions/{owned.id}/")
            self.assertEqual(resp.status_code, 200, resp.content[:400])
            self.assertEqual(str(resp.json()["data"]["id"]), str(owned.id))

        for foreign in (self.other_exp_description, self.other_proj_description):
            denied = self.client.get(f"/api/v1/descriptions/{foreign.id}/")
            self.assertEqual(denied.status_code, 404, denied.content[:400])
