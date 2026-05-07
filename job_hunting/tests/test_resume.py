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


class TestResumeSkillsGroupedNonDev(TestCase):
    """M1: skill_type is now free-form. Verify the model + export pipeline
    preserve PM / BI / PR skill categories that the legacy SkillTag enum
    would have rejected on ingest."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="pmuser", password="pass", first_name="Pat", last_name="Manager"
        )
        self.resume = Resume.objects.create(user=self.user, title="Senior PM")
        # Mix of dev + non-dev categories + one uncategorized to exercise
        # every code path.
        self.skills = [
            ("Stakeholder Management", "Discovery"),
            ("Roadmapping", "Strategy"),
            ("SQL", "Analytics"),
            ("Cross-functional Comms", "Communication"),
            ("Jira", None),
            ("Python", "Language"),
        ]
        for text, skill_type in self.skills:
            s = Skill.objects.create(text=text, skill_type=skill_type)
            ResumeSkill.objects.create(resume=self.resume, skill=s)

    def test_skills_grouped_preserves_non_dev_categories(self):
        groups = self.resume.skills_grouped
        self.assertIn("Discovery", groups)
        self.assertIn("Strategy", groups)
        self.assertIn("Analytics", groups)
        self.assertIn("Communication", groups)
        self.assertIn("Language", groups)

    def test_skills_grouped_buckets_uncategorized_as_other(self):
        groups = self.resume.skills_grouped
        self.assertIn("Other", groups)
        self.assertEqual([s.text for s in groups["Other"]], ["Jira"])

    def test_skills_grouped_first_seen_order(self):
        # Insertion order should drive output order so the resume's own
        # taxonomy survives.
        keys = list(self.resume.skills_grouped.keys())
        self.assertEqual(
            keys,
            ["Discovery", "Strategy", "Analytics", "Communication", "Other", "Language"],
        )

    def test_skills_grouped_contains_skill_models(self):
        groups = self.resume.skills_grouped
        analytics = groups["Analytics"]
        self.assertEqual(len(analytics), 1)
        self.assertEqual(analytics[0].text, "SQL")
        self.assertEqual(analytics[0].skill_type, "Analytics")

    def test_skills_grouped_empty_when_resume_has_no_skills(self):
        empty = Resume.objects.create(user=self.user, title="Empty")
        self.assertEqual(dict(empty.skills_grouped), {})

    def test_export_context_skills_lines_emit_non_dev_categories(self):
        from job_hunting.lib.services.resume_export_context import build_context

        ctx = build_context(self.resume)
        lines = ctx["skills_lines"]
        joined = "\n".join(lines)
        # Every category appears as its own line — none silently dropped.
        self.assertIn("Discovery: Stakeholder Management", joined)
        self.assertIn("Strategy: Roadmapping", joined)
        self.assertIn("Analytics: SQL", joined)
        self.assertIn("Communication: Cross-functional Comms", joined)
        self.assertIn("Languages: Python", joined)  # legacy plural alias still works
        self.assertTrue(ctx["has_skills"])

    def test_export_context_other_bucket_emitted_last(self):
        from job_hunting.lib.services.resume_export_context import build_context

        lines = build_context(self.resume)["skills_lines"]
        other_idx = next(i for i, line in enumerate(lines) if line.startswith("Other:"))
        # Anything explicitly tagged appears before the catch-all bucket.
        self.assertEqual(other_idx, len(lines) - 1)
        self.assertIn("Jira", lines[other_idx])

    def test_legacy_dev_only_resume_still_renders(self):
        """Regression guard: SE resumes that use only the historical five
        categories must still produce the same export shape."""
        from job_hunting.lib.services.resume_export_context import build_context

        se_user = User.objects.create_user(username="seuser", password="pass")
        se_resume = Resume.objects.create(user=se_user, title="SE")
        for text, st in [
            ("Python", "Language"), ("Django", "Framework"),
            ("Postgres", "Database"), ("AWS", "Tool/Platform"),
        ]:
            sk = Skill.objects.create(text=text, skill_type=st)
            ResumeSkill.objects.create(resume=se_resume, skill=sk)
        lines = build_context(se_resume)["skills_lines"]
        joined = "\n".join(lines)
        self.assertIn("Languages: Python", joined)
        self.assertIn("Frameworks: Django", joined)
        self.assertIn("Databases: Postgres", joined)
        self.assertIn("Platforms: AWS", joined)


class TestLegacySkillKeyHelper(TestCase):
    """The DOCX template references hardcoded snake_case context keys.
    `_legacy_skill_key` must keep the historical aliases stable while
    handling new free-form categories without crashing the renderer."""

    def test_historical_aliases_preserved(self):
        from job_hunting.lib.services.resume_export_service import _legacy_skill_key

        self.assertEqual(_legacy_skill_key("Language"), "language_skills")
        self.assertEqual(_legacy_skill_key("Framework"), "framework_skills")
        self.assertEqual(_legacy_skill_key("Database"), "database_skills")
        # Tool/Platform → tool_skills (NOT tool_platform_skills) — the
        # legacy template has the abbreviated key baked in.
        self.assertEqual(_legacy_skill_key("Tool/Platform"), "tool_skills")
        self.assertEqual(_legacy_skill_key("Security"), "security_skills")

    def test_new_categories_are_slugified(self):
        from job_hunting.lib.services.resume_export_service import _legacy_skill_key

        self.assertEqual(_legacy_skill_key("Project Management"), "project_management_skills")
        self.assertEqual(_legacy_skill_key("Communication"), "communication_skills")
        self.assertEqual(_legacy_skill_key("BI Tools"), "bi_tools_skills")
        # Punctuation and double spaces collapse to a single underscore.
        self.assertEqual(_legacy_skill_key("Cross-Functional / Soft"), "cross_functional_soft_skills")

    def test_empty_or_whitespace_falls_back_to_other(self):
        from job_hunting.lib.services.resume_export_service import _legacy_skill_key

        self.assertEqual(_legacy_skill_key(""), "other_skills")
        self.assertEqual(_legacy_skill_key("   "), "other_skills")
        # An input that slugifies to nothing (only punctuation) also lands
        # in the catch-all bucket.
        self.assertEqual(_legacy_skill_key("///"), "other_skills")


class TestSkillOutFreeFormTag(TestCase):
    """SkillOut Pydantic model: tag is a free-form Optional[str]. The
    legacy enum would raise on anything outside five dev categories,
    which destroyed non-dev resumes on ingest."""

    def test_accepts_dev_categories(self):
        from job_hunting.lib.services.ingest_resume import SkillOut

        for tag in ["Language", "Framework", "Database", "Tool/Platform", "Security"]:
            s = SkillOut(text="x", tag=tag)
            self.assertEqual(s.tag, tag)

    def test_accepts_non_dev_categories(self):
        from job_hunting.lib.services.ingest_resume import SkillOut

        # The categories real PM, BI, and PR resumes use. None of these
        # would have survived the old SkillTag enum validator.
        for tag in [
            "Stakeholder Management", "Discovery", "Strategy",
            "Analytics", "Statistics", "Data Modeling", "BI Tools",
            "Strategic Communication", "Media Relations", "Writing",
            "Project Management",
        ]:
            s = SkillOut(text="x", tag=tag)
            self.assertEqual(s.tag, tag)

    def test_none_tag_passes_through(self):
        from job_hunting.lib.services.ingest_resume import SkillOut

        s = SkillOut(text="Jira", tag=None)
        self.assertIsNone(s.tag)

    def test_blank_tag_normalizes_to_none(self):
        from job_hunting.lib.services.ingest_resume import SkillOut

        for blank in ["", "   ", "\t\n"]:
            s = SkillOut(text="x", tag=blank)
            self.assertIsNone(s.tag)

    def test_whitespace_around_tag_is_trimmed(self):
        from job_hunting.lib.services.ingest_resume import SkillOut

        s = SkillOut(text="x", tag="  Communication  ")
        self.assertEqual(s.tag, "Communication")


class TestResumeProfessionField(TestCase):
    """M2: Resume.profession is a free-form CharField that drives both the
    audience-aware ingest prompt (M3) and section ordering (M4)."""

    def setUp(self):
        self.user = User.objects.create_user(username="profuser", password="pass")

    def test_profession_defaults_to_null(self):
        r = Resume.objects.create(user=self.user, title="Resume")
        # Round-trip through the DB so we read what was actually persisted.
        r.refresh_from_db()
        self.assertIsNone(r.profession)

    def test_profession_accepts_canonical_values(self):
        for value in [
            "Software Engineering", "Product Management", "Data / BI",
            "PR / Communications", "Marketing", "Sales", "Operations",
            "Design", "Finance", "Other",
        ]:
            r = Resume.objects.create(user=self.user, title="Resume", profession=value)
            r.refresh_from_db()
            self.assertEqual(r.profession, value)

    def test_profession_accepts_arbitrary_strings(self):
        # The column is free-form; non-canonical values must round-trip.
        r = Resume.objects.create(user=self.user, title="Resume", profession="Academic Research")
        r.refresh_from_db()
        self.assertEqual(r.profession, "Academic Research")


class TestResumeProfessionSerializer(TestCase):
    """M2: profession appears in both full and slim serializer output so
    the Ember model can read/write it through JSON:API."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="serializeruser", password="pass")
        self.client.force_authenticate(user=self.user)

    def test_full_resume_response_includes_profession(self):
        r = Resume.objects.create(
            user=self.user, title="PM Resume", profession="Product Management"
        )
        response = self.client.get(f"/api/v1/resumes/{r.id}/")
        self.assertEqual(response.status_code, 200)
        attrs = response.json()["data"]["attributes"]
        self.assertEqual(attrs["profession"], "Product Management")

    def test_slim_resume_response_includes_profession(self):
        Resume.objects.create(
            user=self.user, title="BI Resume", profession="Data / BI"
        )
        response = self.client.get("/api/v1/resumes/", {"slim": "true"})
        attrs = response.json()["data"][0]["attributes"]
        self.assertIn("profession", attrs)
        self.assertEqual(attrs["profession"], "Data / BI")

    def test_create_resume_with_profession(self):
        payload = {
            "data": {
                "type": "resume",
                "attributes": {"title": "PR Resume", "profession": "PR / Communications"},
            }
        }
        response = self.client.post("/api/v1/resumes/", data=payload, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            response.json()["data"]["attributes"]["profession"],
            "PR / Communications",
        )


class TestAudienceAwareExtractionPrompt(TestCase):
    """M3: build_extraction_prompt(profession) appends an archetype hint
    when the profession matches a known archetype, and falls back to the
    base prompt otherwise. The free-form rule still applies — hints
    suggest, they do not enforce."""

    def test_no_profession_returns_base_prompt(self):
        from job_hunting.lib.services.ingest_resume import (
            build_extraction_prompt, _BASE_EXTRACTION_PROMPT,
        )

        self.assertEqual(build_extraction_prompt(None), _BASE_EXTRACTION_PROMPT)
        self.assertEqual(build_extraction_prompt(""), _BASE_EXTRACTION_PROMPT)
        self.assertEqual(build_extraction_prompt("   "), _BASE_EXTRACTION_PROMPT)

    def test_unknown_profession_falls_back_to_base(self):
        from job_hunting.lib.services.ingest_resume import (
            build_extraction_prompt, _BASE_EXTRACTION_PROMPT,
        )

        # Free-form input — anything outside the canonical list still
        # works; we just don't have category hints for it.
        self.assertEqual(
            build_extraction_prompt("Astrologer"), _BASE_EXTRACTION_PROMPT
        )

    def test_pm_profession_appends_pm_hints(self):
        from job_hunting.lib.services.ingest_resume import build_extraction_prompt

        prompt = build_extraction_prompt("Product Management")
        self.assertIn("Product Management", prompt)
        self.assertIn("Discovery", prompt)
        self.assertIn("Strategy", prompt)
        self.assertIn("Stakeholder Management", prompt)
        # The base rules still appear so the LLM doesn't lose its date /
        # bullet contracts.
        self.assertIn("ORDER", prompt)
        self.assertIn("VERBATIM BULLETS", prompt)

    def test_bi_profession_appends_bi_hints(self):
        from job_hunting.lib.services.ingest_resume import build_extraction_prompt

        prompt = build_extraction_prompt("Data / BI")
        self.assertIn("Analytics", prompt)
        self.assertIn("Statistics", prompt)
        self.assertIn("Data Modeling", prompt)
        self.assertIn("BI Tools", prompt)

    def test_pr_profession_appends_pr_hints(self):
        from job_hunting.lib.services.ingest_resume import build_extraction_prompt

        prompt = build_extraction_prompt("PR / Communications")
        self.assertIn("Strategic Communication", prompt)
        self.assertIn("Media Relations", prompt)
        self.assertIn("Writing", prompt)

    def test_se_profession_keeps_legacy_categories(self):
        """Regression guard: SE resumes still see the historical five
        categories the existing ingest pipeline learned to produce."""
        from job_hunting.lib.services.ingest_resume import build_extraction_prompt

        prompt = build_extraction_prompt("Software Engineering")
        for cat in ["Languages", "Frameworks", "Databases", "Tools/Platforms", "Security"]:
            self.assertIn(cat, prompt)

    def test_hint_does_not_enforce_closed_set(self):
        """The prompt must remind the LLM that emitting other categories
        is allowed — we don't want to recreate the SkillTag enum gate via
        prompt rigidity."""
        from job_hunting.lib.services.ingest_resume import build_extraction_prompt

        prompt = build_extraction_prompt("Product Management")
        # The base prompt's free-form rule survives.
        self.assertIn("free-form", prompt)
        # And the hint paragraph explicitly defers to the resume itself.
        self.assertIn("do not invent categories the resume does not use", prompt)


class TestIngestResumeProfessionWiring(TestCase):
    """M3: IngestResume reads Resume.profession off the pre-created record
    and feeds it to build_extraction_prompt. Verifies the wiring without
    actually calling an LLM."""

    def setUp(self):
        self.user = User.objects.create_user(username="wireuser", password="pass")

    def test_resolve_profession_reads_db_resume(self):
        from job_hunting.lib.services.ingest_resume import IngestResume

        r = Resume.objects.create(
            user=self.user, title="x", profession="Product Management"
        )
        ingester = IngestResume(user=self.user, db_resume=r)
        self.assertEqual(ingester._resolve_profession(), "Product Management")

    def test_resolve_profession_returns_none_when_unset(self):
        from job_hunting.lib.services.ingest_resume import IngestResume

        r = Resume.objects.create(user=self.user, title="x")
        ingester = IngestResume(user=self.user, db_resume=r)
        self.assertIsNone(ingester._resolve_profession())

    def test_resolve_profession_returns_none_without_db_resume(self):
        from job_hunting.lib.services.ingest_resume import IngestResume

        ingester = IngestResume(user=self.user)
        self.assertIsNone(ingester._resolve_profession())

    def test_resolve_profession_strips_whitespace(self):
        from job_hunting.lib.services.ingest_resume import IngestResume

        r = Resume.objects.create(
            user=self.user, title="x", profession="  Marketing  "
        )
        ingester = IngestResume(user=self.user, db_resume=r)
        self.assertEqual(ingester._resolve_profession(), "Marketing")
