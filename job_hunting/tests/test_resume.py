from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status
from job_hunting.models import (
    Resume, Skill, ResumeSkill, Experience, ResumeExperience,
    Education, ResumeEducation, Certification, ResumeCertification,
    Summary, ResumeSummary, Project, ResumeProject,
    Description, ExperienceDescription, ProjectDescription,
    JobApplication, JobPost, Company, Score,
)

User = get_user_model()


class TestResumeModel(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="resumeuser", password="pass")

    def test_create_resume(self):
        r = Resume.objects.create(user=self.user, title="My Resume")
        self.assertEqual(r.title, "My Resume")
        self.assertEqual(r.user, self.user)

    def test_favorite_defaults_false(self):
        r = Resume.objects.create(user=self.user)
        self.assertFalse(r.favorite)

    def test_nullable_fields(self):
        r = Resume.objects.create()
        self.assertIsNone(r.title)
        self.assertIsNone(r.user)
        self.assertIsNone(r.file_path)

    def test_multiple_resumes_per_user(self):
        Resume.objects.create(user=self.user, title="Resume A")
        Resume.objects.create(user=self.user, title="Resume B")
        self.assertEqual(Resume.objects.filter(user=self.user).count(), 2)


class TestResumeAPI(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="resumeapi", password="pass")
        self.client.force_authenticate(user=self.user)
        self.resume = Resume.objects.create(user=self.user, title="Main Resume")

    def test_list_resumes(self):
        response = self.client.get("/api/v1/resumes/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("data", response.json())

    def test_retrieve_resume(self):
        response = self.client.get(f"/api/v1/resumes/{self.resume.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()["data"]
        self.assertEqual(data["attributes"]["title"], "Main Resume")

    def test_create_resume(self):
        payload = {
            "data": {
                "type": "resume",
                "attributes": {"title": "New Resume"},
            }
        }
        response = self.client.post("/api/v1/resumes/", data=payload, format="json")
        self.assertIn(response.status_code, [200, 201])

    def test_update_resume_title(self):
        payload = {
            "data": {
                "type": "resume",
                "id": str(self.resume.id),
                "attributes": {"title": "Updated Resume"},
            }
        }
        response = self.client.patch(
            f"/api/v1/resumes/{self.resume.id}/", data=payload, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resume.refresh_from_db()
        self.assertEqual(self.resume.title, "Updated Resume")

    def test_mark_favorite(self):
        payload = {
            "data": {
                "type": "resume",
                "id": str(self.resume.id),
                "attributes": {"favorite": True},
            }
        }
        response = self.client.patch(
            f"/api/v1/resumes/{self.resume.id}/", data=payload, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resume.refresh_from_db()
        self.assertTrue(self.resume.favorite)

    def test_other_user_cannot_retrieve(self):
        other = User.objects.create_user(username="other_resume", password="pass")
        other_client = APIClient()
        other_client.force_authenticate(user=other)
        response = other_client.get(f"/api/v1/resumes/{self.resume.id}/")
        self.assertIn(response.status_code, [403, 404])


class TestResumeJSONAPIRelationships(TestCase):
    """Verify resume responses use proper JSON:API relationship linkage + sideloading."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="reluser", password="pass")
        self.client.force_authenticate(user=self.user)
        self.resume = Resume.objects.create(user=self.user, title="Test Resume")

        # Create related records via join tables
        self.skill = Skill.objects.create(text="Python", skill_type="technical")
        ResumeSkill.objects.create(resume=self.resume, skill=self.skill, active=True)

        self.experience = Experience.objects.create(title="Developer", location="Remote")
        ResumeExperience.objects.create(resume=self.resume, experience=self.experience, order=0)

        self.education = Education.objects.create(degree="BS", institution="MIT")
        ResumeEducation.objects.create(resume=self.resume, education=self.education)

        self.certification = Certification.objects.create(title="AWS", issuer="Amazon")
        ResumeCertification.objects.create(resume=self.resume, certification=self.certification)

        self.summary = Summary.objects.create(content="A summary", user=self.user)
        ResumeSummary.objects.create(resume=self.resume, summary=self.summary, active=True)

        self.project = Project.objects.create(title="Open Source Tool")
        ResumeProject.objects.create(resume=self.resume, project=self.project, order=0)

    def _get_retrieve(self):
        response = self.client.get(f"/api/v1/resumes/{self.resume.id}/")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def _get_list(self):
        response = self.client.get("/api/v1/resumes/")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_retrieve_has_relationship_data_linkage(self):
        body = self._get_retrieve()
        rels = body["data"]["relationships"]
        for rel_name, rel_type, expected_id in [
            ("skills", "skill", self.skill.id),
            ("experiences", "experience", self.experience.id),
            ("educations", "education", self.education.id),
            ("certifications", "certification", self.certification.id),
            ("summaries", "summary", self.summary.id),
            ("projects", "project", self.project.id),
        ]:
            with self.subTest(rel=rel_name):
                self.assertIn(rel_name, rels, f"Missing relationship: {rel_name}")
                self.assertIn("data", rels[rel_name], f"Missing data key in {rel_name}")
                data = rels[rel_name]["data"]
                self.assertIsInstance(data, list)
                self.assertEqual(len(data), 1)
                self.assertEqual(data[0]["type"], rel_type)
                self.assertEqual(data[0]["id"], str(expected_id))

    def test_retrieve_has_included_sideloads(self):
        body = self._get_retrieve()
        self.assertIn("included", body)
        included_types = {r["type"] for r in body["included"]}
        for expected_type in ["skill", "experience", "education", "certification", "summary", "project"]:
            self.assertIn(expected_type, included_types, f"Missing included type: {expected_type}")

    def test_retrieve_included_has_attributes(self):
        body = self._get_retrieve()
        included_by_type = {}
        for r in body["included"]:
            included_by_type[r["type"]] = r
        self.assertEqual(included_by_type["skill"]["attributes"]["text"], "Python")
        self.assertEqual(included_by_type["experience"]["attributes"]["title"], "Developer")
        self.assertEqual(included_by_type["education"]["attributes"]["degree"], "BS")
        self.assertEqual(included_by_type["certification"]["attributes"]["title"], "AWS")
        self.assertEqual(included_by_type["summary"]["attributes"]["content"], "A summary")
        self.assertEqual(included_by_type["project"]["attributes"]["title"], "Open Source Tool")

    def test_retrieve_no_embedded_attributes(self):
        body = self._get_retrieve()
        attrs = body["data"]["attributes"]
        for key in ["skills", "experiences", "educations", "certifications", "summaries", "projects"]:
            self.assertNotIn(key, attrs, f"Embedded attribute should not exist: {key}")

    def test_retrieve_keeps_summary_convenience_attribute(self):
        body = self._get_retrieve()
        # The active summary content is a convenience attribute (not the relationship)
        self.assertIn("summary", body["data"]["attributes"])

    def test_list_has_relationship_data_linkage(self):
        body = self._get_list()
        self.assertEqual(len(body["data"]), 1)
        rels = body["data"][0]["relationships"]
        self.assertIn("data", rels["skills"])
        self.assertEqual(len(rels["skills"]["data"]), 1)

    def test_list_has_included_sideloads(self):
        body = self._get_list()
        self.assertIn("included", body)
        included_types = {r["type"] for r in body["included"]}
        self.assertIn("skill", included_types)
        self.assertIn("experience", included_types)

    def test_slim_request_skips_linkage(self):
        response = self.client.get("/api/v1/resumes/?slim=1")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        # Slim responses should not have relationship data or included
        self.assertNotIn("included", body)

    def test_empty_resume_has_empty_data_arrays(self):
        empty = Resume.objects.create(user=self.user, title="Empty")
        response = self.client.get(f"/api/v1/resumes/{empty.id}/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        rels = body["data"]["relationships"]
        for rel_name in ["skills", "experiences", "educations", "certifications", "summaries", "projects"]:
            with self.subTest(rel=rel_name):
                self.assertEqual(rels[rel_name]["data"], [])

    def test_user_relationship_has_data(self):
        body = self._get_retrieve()
        user_rel = body["data"]["relationships"]["user"]
        self.assertIn("data", user_rel)
        self.assertEqual(user_rel["data"]["type"], "user")
        self.assertEqual(user_rel["data"]["id"], str(self.user.id))

    def test_other_user_data_not_in_included(self):
        other = User.objects.create_user(username="other_rel", password="pass")
        other_resume = Resume.objects.create(user=other, title="Other")
        other_skill = Skill.objects.create(text="Java")
        ResumeSkill.objects.create(resume=other_resume, skill=other_skill)

        body = self._get_list()
        included_ids = {(r["type"], r["id"]) for r in body.get("included", [])}
        self.assertNotIn(("skill", str(other_skill.id)), included_ids)

    def test_experience_descriptions_have_data_linkage(self):
        desc = Description.objects.create(content="Built microservices")
        ExperienceDescription.objects.create(
            experience=self.experience, description=desc, order=0
        )
        body = self._get_retrieve()
        # Find the experience in included
        exp_resource = next(
            r for r in body["included"]
            if r["type"] == "experience" and r["id"] == str(self.experience.id)
        )
        desc_rel = exp_resource["relationships"]["descriptions"]
        self.assertIn("data", desc_rel)
        self.assertEqual(len(desc_rel["data"]), 1)
        self.assertEqual(desc_rel["data"][0]["type"], "description")
        self.assertEqual(desc_rel["data"][0]["id"], str(desc.id))

    def test_experience_descriptions_sideloaded_in_included(self):
        desc = Description.objects.create(content="Deployed to prod")
        ExperienceDescription.objects.create(
            experience=self.experience, description=desc, order=0
        )
        body = self._get_retrieve()
        included_types = {(r["type"], r["id"]) for r in body["included"]}
        self.assertIn(("description", str(desc.id)), included_types)
        desc_resource = next(
            r for r in body["included"]
            if r["type"] == "description" and r["id"] == str(desc.id)
        )
        self.assertEqual(desc_resource["attributes"]["content"], "Deployed to prod")

    def test_summary_with_null_user_id_included(self):
        """Summaries with user_id=None should still be sideloaded when linked to a resume."""
        # Remove the setUp summary (which has user_id set) and create one without
        ResumeSummary.objects.filter(resume=self.resume).delete()
        orphan_summary = Summary.objects.create(content="Orphan summary", user=None)
        ResumeSummary.objects.create(resume=self.resume, summary=orphan_summary, active=True)

        body = self._get_retrieve()
        included_ids = {(r["type"], r["id"]) for r in body["included"]}
        self.assertIn(("summary", str(orphan_summary.id)), included_ids)
        summary_resource = next(
            r for r in body["included"]
            if r["type"] == "summary" and r["id"] == str(orphan_summary.id)
        )
        self.assertEqual(summary_resource["attributes"]["content"], "Orphan summary")


class TestResumeExportContext(TestCase):
    """Verify to_export_context returns all resume data for Jinja templates."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="exportuser", password="pass", first_name="Jane", last_name="Doe"
        )
        self.resume = Resume.objects.create(user=self.user, title="Senior Engineer")

        self.skill = Skill.objects.create(text="Python", skill_type="Language")
        ResumeSkill.objects.create(resume=self.resume, skill=self.skill)

        self.exp = Experience.objects.create(title="Dev", location="NYC")
        ResumeExperience.objects.create(resume=self.resume, experience=self.exp, order=0)
        self.exp_desc = Description.objects.create(content="Built APIs")
        ExperienceDescription.objects.create(experience=self.exp, description=self.exp_desc, order=0)

        self.edu = Education.objects.create(degree="BS", institution="MIT")
        ResumeEducation.objects.create(resume=self.resume, education=self.edu)

        self.cert = Certification.objects.create(title="AWS", issuer="Amazon")
        ResumeCertification.objects.create(resume=self.resume, certification=self.cert)

        self.summary = Summary.objects.create(content="A great engineer", user=self.user)
        ResumeSummary.objects.create(resume=self.resume, summary=self.summary, active=True)

        self.project = Project.objects.create(title="OSS Tool", user=self.user)
        ResumeProject.objects.create(resume=self.resume, project=self.project, order=0)
        self.proj_desc = Description.objects.create(content="Open source CLI")
        ProjectDescription.objects.create(project=self.project, description=self.proj_desc, order=0)

    def test_export_context_has_all_keys(self):
        ctx = self.resume.to_export_context()
        for key in ["header", "summary", "experiences", "educations", "certifications", "skills", "projects"]:
            self.assertIn(key, ctx, f"Missing key: {key}")

    def test_export_context_header(self):
        ctx = self.resume.to_export_context()
        self.assertEqual(ctx["header"]["name"], "Jane Doe")
        self.assertEqual(ctx["header"]["title"], "Senior Engineer")

    def test_export_context_summary(self):
        ctx = self.resume.to_export_context()
        self.assertEqual(ctx["summary"], "A great engineer")

    def test_export_context_experiences(self):
        ctx = self.resume.to_export_context()
        self.assertEqual(len(ctx["experiences"]), 1)
        self.assertEqual(ctx["experiences"][0]["title"], "Dev")
        self.assertEqual(ctx["experiences"][0]["descriptions"], ["Built APIs"])

    def test_export_context_educations(self):
        ctx = self.resume.to_export_context()
        self.assertEqual(len(ctx["educations"]), 1)
        self.assertEqual(ctx["educations"][0]["degree"], "BS")

    def test_export_context_certifications(self):
        ctx = self.resume.to_export_context()
        self.assertEqual(len(ctx["certifications"]), 1)
        self.assertEqual(ctx["certifications"][0]["title"], "AWS")

    def test_export_context_skills(self):
        ctx = self.resume.to_export_context()
        self.assertEqual(len(ctx["skills"]), 1)
        self.assertEqual(ctx["skills"][0]["text"], "Python")
        self.assertEqual(ctx["skills"][0]["skill_type"], "Language")

    def test_export_context_projects(self):
        ctx = self.resume.to_export_context()
        self.assertEqual(len(ctx["projects"]), 1)
        self.assertEqual(ctx["projects"][0]["title"], "OSS Tool")
        self.assertEqual(ctx["projects"][0]["description"], ["Open source CLI"])

    def test_export_context_empty_resume(self):
        empty = Resume.objects.create(user=self.user, title="Empty")
        ctx = empty.to_export_context()
        self.assertEqual(ctx["experiences"], [])
        self.assertEqual(ctx["educations"], [])
        self.assertEqual(ctx["certifications"], [])
        self.assertEqual(ctx["skills"], [])
        self.assertEqual(ctx["projects"], [])


class TestResumeSlimMeta(TestCase):
    """Verify slim list responses include meta counts per JSON:API spec."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="slimuser", password="pass")
        self.client.force_authenticate(user=self.user)
        self.resume = Resume.objects.create(user=self.user, title="Slim Resume")

        # Related records
        self.skill = Skill.objects.create(text="Go", skill_type="Language")
        ResumeSkill.objects.create(resume=self.resume, skill=self.skill)

        self.experience = Experience.objects.create(title="Engineer")
        ResumeExperience.objects.create(resume=self.resume, experience=self.experience, order=0)

        self.company = Company.objects.create(name="SlimCo")
        self.job_post = JobPost.objects.create(
            title="Dev", company=self.company, created_by=self.user
        )
        JobApplication.objects.create(
            job_post=self.job_post, resume=self.resume, user=self.user
        )
        JobApplication.objects.create(
            job_post=self.job_post, resume=self.resume, user=self.user
        )
        Score.objects.create(
            job_post=self.job_post, resume=self.resume, user=self.user, score=85
        )

    def test_slim_list_has_meta_counts(self):
        response = self.client.get("/api/v1/resumes/", {"slim": "true"})
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(len(data), 1)
        resource = data[0]
        self.assertIn("meta", resource)
        meta = resource["meta"]
        self.assertEqual(meta["job_application_count"], 2)
        self.assertEqual(meta["score_count"], 1)
        self.assertEqual(meta["experience_count"], 1)
        self.assertEqual(meta["skill_count"], 1)

    def test_slim_list_only_has_slim_attributes(self):
        response = self.client.get("/api/v1/resumes/", {"slim": "true"})
        data = response.json()["data"][0]
        attrs = data["attributes"]
        self.assertIn("name", attrs)
        self.assertIn("title", attrs)
        self.assertIn("notes", attrs)
        self.assertIn("favorite", attrs)
        self.assertNotIn("file_path", attrs)
        self.assertNotIn("user_id", attrs)

    def test_slim_list_has_no_included(self):
        response = self.client.get("/api/v1/resumes/", {"slim": "true"})
        self.assertNotIn("included", response.json())

    def test_non_slim_list_has_no_meta_counts(self):
        response = self.client.get("/api/v1/resumes/")
        data = response.json()["data"][0]
        self.assertNotIn("meta", data)

    def test_slim_empty_resume_has_zero_counts(self):
        empty = Resume.objects.create(user=self.user, title="Empty")
        response = self.client.get("/api/v1/resumes/", {"slim": "true"})
        data = response.json()["data"]
        empty_resource = next(r for r in data if r["id"] == str(empty.id))
        meta = empty_resource["meta"]
        self.assertEqual(meta["job_application_count"], 0)
        self.assertEqual(meta["score_count"], 0)
        self.assertEqual(meta["experience_count"], 0)
        self.assertEqual(meta["skill_count"], 0)
