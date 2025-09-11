from rest_framework.test import APITestCase
from rest_framework import status

from job_hunting.lib.db import init_sqlalchemy
from job_hunting.lib.models.base import BaseModel, Base
from job_hunting.lib.models import (
    User, Resume, Score, JobPost, Scrape, Company, CoverLetter, Application
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
        # Hard reset tables for isolation between tests
        Base.metadata.drop_all(bind=self.engine)
        Base.metadata.create_all(bind=self.engine)

    def test_create_and_list_users_jsonapi(self):
        # Initially empty list
        resp = self.client.get("/api/v1/users/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data.get("data"), [])

        # Create a user via JSON:API payload
        payload = {
            "data": {
                "type": "users",
                "attributes": {"name": "Alice", "email": "alice@example.com"},
            }
        }
        resp = self.client.post("/api/v1/users/", data=payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["data"]["type"], "users")
        user_id = int(resp.data["data"]["id"])

        # List again, now has one record
        resp = self.client.get("/api/v1/users/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 1)
        self.assertEqual(resp.data["data"][0]["id"], str(user_id))

    def test_resume_creation_with_user_relationship_and_scoped_route(self):
        # Prepare a user
        session = self.session
        user = User(name="Bob", email="bob@example.com")
        session.add(user)
        session.commit()

        # Create resume with belongs-to relationship via JSON:API
        payload = {
            "data": {
                "type": "resumes",
                "attributes": {"content": "My resume body", "file_path": "/tmp/r.txt"},
                "relationships": {
                    "user": {"data": {"type": "users", "id": str(user.id)}}
                },
            }
        }
        resp = self.client.post("/api/v1/resumes/", data=payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        resume_id = int(resp.data["data"]["id"])
        self.assertEqual(resp.data["data"]["type"], "resumes")
        # Scoped route: /users/{id}/resumes
        resp = self.client.get(f"/api/v1/users/{user.id}/resumes/")
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

        user = User(name="Eve", email="eve@example.com")
        session.add(user)
        session.commit()

        resume = Resume(content="Eve resume", user_id=user.id, file_path="/tmp/eve.txt")
        session.add(resume)
        session.commit()

        job = JobPost(title="Analyst", description="Analyze", company_id=company.id)
        session.add(job)
        session.commit()

        scrape = Scrape(url="https://example.com/job", company_id=company.id, job_post_id=job.id)
        session.add(scrape)
        session.commit()

        cover = CoverLetter(content="Dear HR", user_id=user.id, resume_id=resume.id, job_post_id=job.id)
        session.add(cover)
        session.commit()

        app = Application(user_id=user.id, job_post_id=job.id, resume_id=resume.id, cover_letter_id=cover.id, status="submitted")
        session.add(app)
        session.commit()

        # /job-posts/{id}/scrapes
        resp = self.client.get(f"/api/v1/job-posts/{job.id}/scrapes/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 1)
        self.assertEqual(resp.data["data"][0]["type"], "scrapes")

        # /job-posts/{id}/cover-letters
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

        user = User(name="Carl", email="carl@example.com")
        session.add(user)
        session.commit()

        company = Company(name="Initech", display_name="Initech LLC")
        session.add(company)
        session.commit()

        job = JobPost(title="TPS Consultant", description="TPS work", company_id=company.id)
        session.add(job)
        session.commit()

        app = Application(user_id=user.id, job_post_id=job.id, status="submitted")
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

        user = User(name="Dana", email="dana@example.com")
        session.add(user)
        session.commit()

        resume = Resume(content="Dana resume", user_id=user.id, file_path="/tmp/dana.txt")
        session.add(resume)
        session.commit()

        company = Company(name="Umbrella", display_name="Umbrella Corp")
        session.add(company)
        session.commit()

        job = JobPost(title="Security", description="Keep safe", company_id=company.id)
        session.add(job)
        session.commit()

        cover = CoverLetter(content="Cover Content", user_id=user.id, resume_id=resume.id, job_post_id=job.id)
        session.add(cover)
        session.commit()

        app = Application(user_id=user.id, job_post_id=job.id, resume_id=resume.id, cover_letter_id=cover.id, status="submitted")
        session.add(app)
        session.commit()

        # /resumes/{id}/cover-letters
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

        user = User(name="Frank", email="frank@example.com")
        session.add(user)
        session.commit()

        resume = Resume(content="R", user_id=user.id, file_path="/tmp/r.txt")
        session.add(resume)
        session.commit()

        company = Company(name="Hooli", display_name="Hooli Inc")
        session.add(company)
        session.commit()

        job = JobPost(title="Dev", description="Code", company_id=company.id)
        session.add(job)
        session.commit()

        score = Score(score=99, explanation="Excellent", resume_id=resume.id, job_post_id=job.id, user_id=user.id)
        session.add(score)
        session.commit()

        resp = self.client.get(f"/api/v1/users/{user.id}/scores/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["data"]), 1)
        self.assertEqual(resp.data["data"][0]["type"], "scores")
