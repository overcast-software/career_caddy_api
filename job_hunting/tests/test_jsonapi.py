from django.contrib.auth import get_user_model
from rest_framework.test import APITransactionTestCase
from rest_framework import status

from job_hunting.lib.db import init_sqlalchemy
from job_hunting.lib.models.base import BaseModel, Base
from job_hunting.models import Company, JobPost, Resume, Score, CoverLetter, Application, Scrape


class JSONAPITests(APITransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Ensure SQLAlchemy is initialized and tables exist in the Django test DB
        init_sqlalchemy()
        cls.session = BaseModel.get_session()
        cls.engine = cls.session.bind
        Base.metadata.create_all(bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        # Release SA session without closing underlying connections Django may reuse
        try:
            if hasattr(cls.session, "remove"):
                cls.session.remove()
        except Exception:
            pass
        super().tearDownClass()

    def setUp(self):
        # Release any open SA connections/transactions before DDL to avoid lock contention
        self.session.close()
        if hasattr(self.session, "remove"):
            self.session.remove()

        # Hard reset SA tables for isolation between tests (exclude auth_user — Django-owned)
        sa_tables = [t for t in Base.metadata.sorted_tables if t.name != "auth_user"]
        Base.metadata.drop_all(bind=self.engine, tables=sa_tables)
        Base.metadata.create_all(bind=self.engine)

        # Reinitialize session after remove()
        self.session = BaseModel.get_session()

        # Create a Django user and authenticate with JWT for protected endpoints
        User = get_user_model()
        self.username = "testuser"
        self.password = "testpass123"
        self.user = User.objects.create_user(
            username=self.username, email="testuser@example.com", password=self.password
        )
        token = self._obtain_jwt(self.username, self.password)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def _obtain_jwt(self, username, password):
        resp = self.client.post(
            "/api/v1/token/",
            data={"username": username, "password": password},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        return resp.data["access"]

    def test_create_and_list_users_jsonapi(self):
        # Unauthenticated requests should be rejected for listing users
        self.client.credentials(HTTP_AUTHORIZATION="")
        resp = self.client.get("/api/v1/users/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

        # Create a user via JSON:API payload (registration is open)
        payload = {
            "data": {
                "type": "users",
                "attributes": {
                    "username": "alice",
                    "email": "alice@example.com",
                    "password": "s3cr3tpass",
                    "first_name": "Alice",
                    "last_name": "Anderson",
                },
            }
        }
        resp = self.client.post("/api/v1/users/", data=payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertIn(resp.data["data"]["type"], ("user", "user"))
        user_id = int(resp.data["data"]["id"])

        # Authenticate as the newly created user and list (should only see self)
        token = self._obtain_jwt("alice", "s3cr3tpass")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        resp = self.client.get("/api/v1/users/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 1)
        self.assertEqual(resp.data["data"][0]["id"], str(user_id))

    def test_resume_creation_with_user_relationship_and_scoped_route(self):
        # Create resume with belongs-to relationship via JSON:API for the authenticated user
        payload = {
            "data": {
                "type": "resumes",
                "attributes": {"file_path": "/tmp/r.txt"},
                "relationships": {
                    "user": {"data": {"type": "users", "id": str(self.user.id)}}
                },
            }
        }
        resp = self.client.post("/api/v1/resumes/", data=payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        resume_id = int(resp.data["data"]["id"])
        self.assertEqual(resp.data["data"]["type"], "resume")
        # Scoped route: /users/{id}/resumes
        resp = self.client.get(f"/api/v1/users/{self.user.id}/resumes/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [int(r["id"]) for r in resp.data["data"]]
        self.assertIn(resume_id, ids)

    def test_company_job_posts_scoped_and_job_post_relationship_linkage(self):

        # Seed company and job post via Django ORM
        company = Company.objects.create(name="ACME", display_name="ACME Corp")
        job = JobPost.objects.create(
            title="Engineer", description="Build things", company=company,
            created_by=self.user,
        )

        # Scoped route: /companies/{id}/job-posts
        resp = self.client.get(f"/api/v1/companies/{company.id}/job-posts/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 1)
        self.assertEqual(resp.data["data"][0]["type"], "job-post")
        self.assertEqual(resp.data["data"][0]["id"], str(job.id))

        # Add score to job post and check linkage endpoint
        score = Score.objects.create(score=88, explanation="Good fit", job_post_id=job.id)

        # Relationship linkage: /job-posts/{id}/relationships/scores
        resp = self.client.get(f"/api/v1/job-posts/{job.id}/relationships/scores/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(isinstance(resp.data["data"], list))
        self.assertEqual(resp.data["data"][0]["type"], "score")
        self.assertEqual(resp.data["data"][0]["id"], str(score.id))

    def test_job_post_child_routes_scrapes_cover_letters_applications(self):

        company = Company.objects.create(name="Globex", display_name="Globex Inc")

        # Resume owned by authenticated Django user
        resume = Resume.objects.create(user_id=self.user.id, file_path="/tmp/eve.txt")

        job = JobPost.objects.create(title="Analyst", description="Analyze", company=company, created_by=self.user)

        Scrape.objects.create(
            url="https://example.com/job", company_id=company.id, job_post_id=job.id
        )

        cover = CoverLetter.objects.create(
            content="Dear HR",
            user_id=self.user.id,
            resume_id=resume.id,
            job_post_id=job.id,
        )

        Application.objects.create(
            user_id=self.user.id,
            job_post_id=job.id,
            resume_id=resume.id,
            cover_letter_id=cover.id,
            status="submitted",
        )

        # /job-posts/{id}/scrapes
        resp = self.client.get(f"/api/v1/job-posts/{job.id}/scrapes/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 1)
        self.assertEqual(resp.data["data"][0]["type"], "scrape")

        # /job-posts/{id}/cover-letters (only those owned by current user)
        resp = self.client.get(f"/api/v1/job-posts/{job.id}/cover-letters/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 1)
        self.assertEqual(resp.data["data"][0]["type"], "cover-letter")

        # /job-posts/{id}/job-applications
        resp = self.client.get(f"/api/v1/job-posts/{job.id}/job-applications/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 1)
        self.assertEqual(resp.data["data"][0]["type"], "job-application")

    def test_update_and_delete_application_via_jsonapi(self):

        company = Company.objects.create(name="Initech", display_name="Initech LLC")
        job = JobPost.objects.create(
            title="TPS Consultant", description="TPS work", company=company,
            created_by=self.user,
        )

        app = Application.objects.create(user_id=self.user.id, job_post_id=job.id, status="submitted")

        # PATCH (partial update) JSON:API
        payload = {
            "data": {
                "type": "applications",
                "id": str(app.id),
                "attributes": {"status": "interview"},
            }
        }
        resp = self.client.patch(
            f"/api/v1/job-applications/{app.id}/", data=payload, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["data"]["attributes"]["status"], "interview")

        # DELETE
        resp = self.client.delete(f"/api/v1/job-applications/{app.id}/")
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

        # Verify deletion
        resp = self.client.get(f"/api/v1/job-applications/{app.id}/")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_resume_scoped_cover_letters_and_applications(self):
        resume = Resume.objects.create(user_id=self.user.id, file_path="/tmp/dana.txt")

        company = Company.objects.create(name="Umbrella", display_name="Umbrella Corp")
        job = JobPost.objects.create(title="Security", description="Keep safe", company=company, created_by=self.user)

        cover = CoverLetter.objects.create(
            content="Cover Content",
            user_id=self.user.id,
            resume_id=resume.id,
            job_post_id=job.id,
        )

        Application.objects.create(
            user_id=self.user.id,
            job_post_id=job.id,
            resume_id=resume.id,
            cover_letter_id=cover.id,
            status="submitted",
        )

        # /resumes/{id}/cover-letters (owned by current user)
        resp = self.client.get(f"/api/v1/resumes/{resume.id}/cover-letters/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 1)
        self.assertEqual(resp.data["data"][0]["type"], "cover-letter")

        # /resumes/{id}/job-applications
        resp = self.client.get(f"/api/v1/resumes/{resume.id}/job-applications/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 1)
        self.assertEqual(resp.data["data"][0]["type"], "job-application")

    def test_user_scoped_scores(self):
        resume = Resume.objects.create(user_id=self.user.id, file_path="/tmp/r.txt")

        company = Company.objects.create(name="Hooli", display_name="Hooli Inc")
        job = JobPost.objects.create(title="Dev", description="Code", company=company, created_by=self.user)

        Score.objects.create(
            score=99,
            explanation="Excellent",
            resume_id=resume.id,
            job_post_id=job.id,
            user_id=self.user.id,
        )

        resp = self.client.get(f"/api/v1/users/{self.user.id}/scores/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 1)
        self.assertEqual(resp.data["data"][0]["type"], "score")

    def test_job_post_datetime_parsing_and_created_at_protection(self):
        company = Company.objects.create(name="TestCorp", display_name="Test Corp")
        job = JobPost.objects.create(
            title="Engineer", description="Build things", company=company,
            created_by=self.user,
        )
        original_created_at = job.created_at

        # Test valid ISO datetime with Z timezone
        payload = {
            "data": {
                "type": "job-posts",
                "id": str(job.id),
                "attributes": {
                    "posted_date": "2025-10-25T09:37:33.140Z",
                    "created_at": "2025-10-25T09:37:33.140Z",  # Should be ignored
                },
            }
        }
        resp = self.client.patch(
            f"/api/v1/job-posts/{job.id}/", data=payload, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        # Reload job from database
        job.refresh_from_db()

        # Verify posted_date was parsed and stored as datetime
        self.assertIsNotNone(job.posted_date)
        self.assertEqual(job.posted_date.year, 2025)
        self.assertEqual(job.posted_date.month, 10)
        self.assertEqual(job.posted_date.day, 25)

        # Verify created_at was not changed
        self.assertEqual(job.created_at, original_created_at)

    def test_job_post_invalid_datetime_returns_400(self):
        company = Company.objects.create(name="TestCorp", display_name="Test Corp")
        job = JobPost.objects.create(
            title="Engineer", description="Build things", company=company,
            created_by=self.user,
        )

        # Test invalid posted_date
        payload = {
            "data": {
                "type": "job-posts",
                "id": str(job.id),
                "attributes": {"posted_date": "not a date"},
            }
        }
        resp = self.client.patch(
            f"/api/v1/job-posts/{job.id}/", data=payload, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid posted_date", str(resp.data))

        # Test invalid extraction_date
        payload = {
            "data": {
                "type": "job-posts",
                "id": str(job.id),
                "attributes": {"extraction_date": "invalid date"},
            }
        }
        resp = self.client.patch(
            f"/api/v1/job-posts/{job.id}/", data=payload, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid extraction_date", str(resp.data))

    def test_job_post_null_datetime_allowed(self):
        company = Company.objects.create(name="TestCorp", display_name="Test Corp")
        job = JobPost.objects.create(
            title="Engineer", description="Build things", company=company,
            created_by=self.user,
        )

        # Test null posted_date (should be allowed)
        payload = {
            "data": {
                "type": "job-posts",
                "id": str(job.id),
                "attributes": {"posted_date": None},
            }
        }
        resp = self.client.patch(
            f"/api/v1/job-posts/{job.id}/", data=payload, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        # Reload and verify
        job.refresh_from_db()
        self.assertIsNone(job.posted_date)
