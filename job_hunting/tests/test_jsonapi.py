from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase
from rest_framework import status

from job_hunting.lib.db import init_sqlalchemy
from job_hunting.lib.models.base import BaseModel, Base
from job_hunting.lib.models import (
    Resume, Score, JobPost, Scrape, Company, CoverLetter, Application
)


class JSONAPITests(APITestCase):
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
        # Clean up tables after all tests in this class
        Base.metadata.drop_all(bind=cls.engine)
        super().tearDownClass()

    def setUp(self):
        # Hard reset SA tables for isolation between tests
        Base.metadata.drop_all(bind=self.engine)
        Base.metadata.create_all(bind=self.engine)

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
        self.assertIn(resp.data["data"]["type"], ("user", "users"))
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
        self.assertEqual(resp.data["data"]["type"], "resumes")
        # Scoped route: /users/{id}/resumes
        resp = self.client.get(f"/api/v1/users/{self.user.id}/resumes/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = [int(r["id"]) for r in resp.data["data"]]
        self.assertIn(resume_id, ids)

    def test_company_job_posts_scoped_and_job_post_relationship_linkage(self):
        session = self.session

        # Seed company and job post
        company = Company(name="ACME", display_name="ACME Corp")
        session.add(company)
        session.commit()

        job = JobPost(title="Engineer", description="Build things", company_id=company.id)
        session.add(job)
        session.commit()

        # Scoped route: /companies/{id}/job-posts
        resp = self.client.get(f"/api/v1/companies/{company.id}/job-posts/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 1)
        self.assertEqual(resp.data["data"][0]["type"], "job-posts")
        self.assertEqual(resp.data["data"][0]["id"], str(job.id))

        # Add score to job post and check linkage endpoint
        score = Score(score=88, explanation="Good fit", job_post_id=job.id)
        session.add(score)
        session.commit()

        # Relationship linkage: /job-posts/{id}/relationships/scores
        resp = self.client.get(f"/api/v1/job-posts/{job.id}/relationships/scores")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(isinstance(resp.data["data"], list))
        self.assertEqual(resp.data["data"][0]["type"], "scores")
        self.assertEqual(resp.data["data"][0]["id"], str(score.id))

    def test_job_post_child_routes_scrapes_cover_letters_applications(self):
        session = self.session

        company = Company(name="Globex", display_name="Globex Inc")
        session.add(company)
        session.commit()

        # Resume owned by authenticated Django user
        resume = Resume(user_id=self.user.id, file_path="/tmp/eve.txt")
        session.add(resume)
        session.commit()

        job = JobPost(title="Analyst", description="Analyze", company_id=company.id)
        session.add(job)
        session.commit()

        scrape = Scrape(url="https://example.com/job", company_id=company.id, job_post_id=job.id)
        session.add(scrape)
        session.commit()

        cover = CoverLetter(content="Dear HR", user_id=self.user.id, resume_id=resume.id, job_post_id=job.id)
        session.add(cover)
        session.commit()

        app = Application(user_id=self.user.id, job_post_id=job.id, resume_id=resume.id, cover_letter_id=cover.id, status="submitted")
        session.add(app)
        session.commit()

        # /job-posts/{id}/scrapes
        resp = self.client.get(f"/api/v1/job-posts/{job.id}/scrapes/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 1)
        self.assertEqual(resp.data["data"][0]["type"], "scrapes")

        # /job-posts/{id}/cover-letters (only those owned by current user)
        resp = self.client.get(f"/api/v1/job-posts/{job.id}/cover-letters/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 1)
        self.assertEqual(resp.data["data"][0]["type"], "cover-letters")

        # /job-posts/{id}/applications
        resp = self.client.get(f"/api/v1/job-posts/{job.id}/applications/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 1)
        self.assertEqual(resp.data["data"][0]["type"], "applications")

    def test_update_and_delete_application_via_jsonapi(self):
        session = self.session

        company = Company(name="Initech", display_name="Initech LLC")
        session.add(company)
        session.commit()

        job = JobPost(title="TPS Consultant", description="TPS work", company_id=company.id)
        session.add(job)
        session.commit()

        app = Application(user_id=self.user.id, job_post_id=job.id, status="submitted")
        session.add(app)
        session.commit()

        # PATCH (partial update) JSON:API
        payload = {
            "data": {
                "type": "applications",
                "id": str(app.id),
                "attributes": {"status": "interview"},
            }
        }
        resp = self.client.patch(f"/api/v1/applications/{app.id}/", data=payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["data"]["attributes"]["status"], "interview")

        # DELETE
        resp = self.client.delete(f"/api/v1/applications/{app.id}/")
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

        # Verify deletion
        resp = self.client.get(f"/api/v1/applications/{app.id}/")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_resume_scoped_cover_letters_and_applications(self):
        session = self.session

        resume = Resume(user_id=self.user.id, file_path="/tmp/dana.txt")
        session.add(resume)
        session.commit()

        company = Company(name="Umbrella", display_name="Umbrella Corp")
        session.add(company)
        session.commit()

        job = JobPost(title="Security", description="Keep safe", company_id=company.id)
        session.add(job)
        session.commit()

        cover = CoverLetter(content="Cover Content", user_id=self.user.id, resume_id=resume.id, job_post_id=job.id)
        session.add(cover)
        session.commit()

        app = Application(user_id=self.user.id, job_post_id=job.id, resume_id=resume.id, cover_letter_id=cover.id, status="submitted")
        session.add(app)
        session.commit()

        # /resumes/{id}/cover-letters (owned by current user)
        resp = self.client.get(f"/api/v1/resumes/{resume.id}/cover-letters/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 1)
        self.assertEqual(resp.data["data"][0]["type"], "cover-letters")

        # /resumes/{id}/applications
        resp = self.client.get(f"/api/v1/resumes/{resume.id}/applications/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 1)
        self.assertEqual(resp.data["data"][0]["type"], "applications")

    def test_user_scoped_scores(self):
        session = self.session

        resume = Resume(user_id=self.user.id, file_path="/tmp/r.txt")
        session.add(resume)
        session.commit()

        company = Company(name="Hooli", display_name="Hooli Inc")
        session.add(company)
        session.commit()

        job = JobPost(title="Dev", description="Code", company_id=company.id)
        session.add(job)
        session.commit()

        score = Score(score=99, explanation="Excellent", resume_id=resume.id, job_post_id=job.id, user_id=self.user.id)
        session.add(score)
        session.commit()

        resp = self.client.get(f"/api/v1/users/{self.user.id}/scores/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 1)
        self.assertEqual(resp.data["data"][0]["type"], "scores")

    def test_job_post_datetime_parsing_and_created_at_protection(self):
        session = self.session

        company = Company(name="TestCorp", display_name="Test Corp")
        session.add(company)
        session.commit()

        job = JobPost(title="Engineer", description="Build things", company_id=company.id)
        session.add(job)
        session.commit()

        original_created_at = job.created_at

        # Test valid ISO datetime with Z timezone
        payload = {
            "data": {
                "type": "job-posts",
                "id": str(job.id),
                "attributes": {
                    "posted_date": "2025-10-25T09:37:33.140Z",
                    "created_at": "2025-10-25T09:37:33.140Z"  # Should be ignored
                },
            }
        }
        resp = self.client.patch(f"/api/v1/job-posts/{job.id}/", data=payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        # Reload job from database
        session.refresh(job)

        # Verify posted_date was parsed and stored as datetime
        self.assertIsNotNone(job.posted_date)
        self.assertEqual(job.posted_date.year, 2025)
        self.assertEqual(job.posted_date.month, 10)
        self.assertEqual(job.posted_date.day, 25)

        # Verify created_at was not changed
        self.assertEqual(job.created_at, original_created_at)

    def test_job_post_invalid_datetime_returns_400(self):
        session = self.session

        company = Company(name="TestCorp", display_name="Test Corp")
        session.add(company)
        session.commit()

        job = JobPost(title="Engineer", description="Build things", company_id=company.id)
        session.add(job)
        session.commit()

        # Test invalid posted_date
        payload = {
            "data": {
                "type": "job-posts",
                "id": str(job.id),
                "attributes": {"posted_date": "not a date"},
            }
        }
        resp = self.client.patch(f"/api/v1/job-posts/{job.id}/", data=payload, format="json")
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
        resp = self.client.patch(f"/api/v1/job-posts/{job.id}/", data=payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid extraction_date", str(resp.data))

    def test_job_post_null_datetime_allowed(self):
        session = self.session

        company = Company(name="TestCorp", display_name="Test Corp")
        session.add(company)
        session.commit()

        job = JobPost(title="Engineer", description="Build things", company_id=company.id)
        session.add(job)
        session.commit()

        # Test null posted_date (should be allowed)
        payload = {
            "data": {
                "type": "job-posts",
                "id": str(job.id),
                "attributes": {"posted_date": None},
            }
        }
        resp = self.client.patch(f"/api/v1/job-posts/{job.id}/", data=payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        # Reload and verify
        session.refresh(job)
        self.assertIsNone(job.posted_date)
