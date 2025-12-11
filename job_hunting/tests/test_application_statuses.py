from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient, APITestCase
from job_hunting.lib.models import (
    Application,
    Status,
    Resume,
    JobPost,
    Company,
    JobApplicationStatus,
)
from job_hunting.lib.models.base import BaseModel, Base
from job_hunting.lib.database import init_sqlalchemy


class ApplicationStatusesTestCase(APITestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Initialize SQLAlchemy to use Django test database
        init_sqlalchemy()
        cls.session = BaseModel.get_session()
        # Create tables if needed
        Base.metadata.create_all(bind=cls.session.bind)

    def setUp(self):
        # Create Django user
        User = get_user_model()
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123"
        )
        
        # Setup API client with authentication
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        
        # Create minimal SQLAlchemy records
        self.company = Company(name="Test Company")
        self.session.add(self.company)
        self.session.commit()
        
        self.job_post = JobPost(
            title="Test Job",
            description="Test job description",
            company_id=self.company.id
        )
        self.session.add(self.job_post)
        self.session.commit()
        
        self.resume = Resume(
            title="Test Resume",
            user_id=self.user.id
        )
        self.session.add(self.resume)
        self.session.commit()
        
        self.application = Application(
            user_id=self.user.id,
            job_post_id=self.job_post.id,
            resume_id=self.resume.id
        )
        self.session.add(self.application)
        self.session.commit()

    def test_post_status_with_relationship_id_updates_application_and_is_listed(self):
        # Arrange: Create a Status row
        status = Status(status="applied", status_type="in_progress")
        self.session.add(status)
        self.session.commit()
        
        # Act: POST to statuses endpoint with relationship ID
        payload = {
            "data": {
                "type": "job-application-status",
                "relationships": {
                    "status": {
                        "data": {
                            "type": "status",
                            "id": str(status.id)
                        }
                    }
                },
                "attributes": {
                    "note": "Submitted via portal"
                }
            }
        }
        
        response = self.client.post(
            f"/api/v1/job-applications/{self.application.id}/statuses",
            data=payload,
            format="json"
        )
        
        # Assert: Response is 201 and properly structured
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["data"]["type"], "job-application-status")
        self.assertEqual(
            response.data["data"]["relationships"]["application"]["data"]["id"],
            str(self.application.id)
        )
        
        # Assert: Application status is updated
        self.session.refresh(self.application)
        self.assertEqual(self.application.status, "applied")
        
        # Assert: GET application shows updated status
        app_response = self.client.get(f"/api/v1/job-applications/{self.application.id}")
        self.assertEqual(app_response.data["data"]["attributes"]["status"], "applied")
        
        # Assert: GET statuses returns one item
        statuses_response = self.client.get(
            f"/api/v1/job-applications/{self.application.id}/statuses"
        )
        self.assertEqual(len(statuses_response.data["data"]), 1)

    def test_post_status_with_string_creates_vocab_and_sets_latest(self):
        # Act: POST with status string
        payload = {
            "data": {
                "type": "job-application-status",
                "attributes": {
                    "status": "Phone Screen",
                    "status_type": "in_progress",
                    "note": "Phone interview scheduled"
                }
            }
        }
        
        response = self.client.post(
            f"/api/v1/job-applications/{self.application.id}/statuses",
            data=payload,
            format="json"
        )
        
        # Assert: Response is 201
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["data"]["type"], "job-application-status")
        
        # Assert: Status row exists with lowercased value
        status = self.session.query(Status).filter_by(
            status="phone screen",
            status_type="in_progress"
        ).first()
        self.assertIsNotNone(status)
        
        # Assert: Application status is updated
        app_response = self.client.get(f"/api/v1/job-applications/{self.application.id}")
        self.assertEqual(app_response.data["data"]["attributes"]["status"], "phone screen")
        
        # Then: POST again with different status
        payload2 = {
            "data": {
                "type": "job-application-status",
                "attributes": {
                    "status": "Rejected",
                    "note": "Not a good fit"
                }
            }
        }
        
        response2 = self.client.post(
            f"/api/v1/job-applications/{self.application.id}/statuses",
            data=payload2,
            format="json"
        )
        
        # Assert: Latest status is reflected
        app_response2 = self.client.get(f"/api/v1/job-applications/{self.application.id}")
        self.assertEqual(app_response2.data["data"]["attributes"]["status"], "rejected")

    def test_statuses_history_returns_full_list_and_can_include_status_resources(self):
        # Arrange: Create two statuses via POSTs
        payload1 = {
            "data": {
                "type": "job-application-status",
                "attributes": {
                    "status": "applied",
                    "note": "Initial application"
                }
            }
        }
        
        payload2 = {
            "data": {
                "type": "job-application-status",
                "attributes": {
                    "status": "interview",
                    "note": "Scheduled for interview"
                }
            }
        }
        
        self.client.post(
            f"/api/v1/job-applications/{self.application.id}/statuses",
            data=payload1,
            format="json"
        )
        
        self.client.post(
            f"/api/v1/job-applications/{self.application.id}/statuses",
            data=payload2,
            format="json"
        )
        
        # Act: GET statuses with include
        response = self.client.get(
            f"/api/v1/job-applications/{self.application.id}/statuses?include=status"
        )
        
        # Assert: Response contains full list
        self.assertEqual(len(response.data["data"]), 2)
        
        # Assert: Each item has required attributes
        for item in response.data["data"]:
            self.assertIn("created_at", item["attributes"])
            self.assertIn("note", item["attributes"])
        
        # Assert: Included contains status resources
        self.assertIn("included", response.data)
        status_resources = [
            item for item in response.data["included"] 
            if item["type"] == "status"
        ]
        self.assertEqual(len(status_resources), 2)
        
        # Assert: Status values are as expected
        status_values = {
            resource["attributes"]["status"] 
            for resource in status_resources
        }
        self.assertEqual(status_values, {"applied", "interview"})

    def test_legacy_application_statuses_endpoint_still_works(self):
        # Arrange: Create a status
        payload = {
            "data": {
                "type": "job-application-status",
                "attributes": {
                    "status": "applied",
                    "note": "Test status"
                }
            }
        }
        
        self.client.post(
            f"/api/v1/job-applications/{self.application.id}/statuses",
            data=payload,
            format="json"
        )
        
        # Act: GET legacy endpoint
        legacy_response = self.client.get(
            f"/api/v1/job-applications/{self.application.id}/application-statuses"
        )
        
        # Act: GET new endpoint
        new_response = self.client.get(
            f"/api/v1/job-applications/{self.application.id}/statuses"
        )
        
        # Assert: Both endpoints return same count
        self.assertEqual(legacy_response.status_code, 200)
        self.assertEqual(new_response.status_code, 200)
        self.assertEqual(
            len(legacy_response.data["data"]),
            len(new_response.data["data"])
        )
        
        # Assert: Resource IDs match
        legacy_ids = {item["id"] for item in legacy_response.data["data"]}
        new_ids = {item["id"] for item in new_response.data["data"]}
        self.assertEqual(legacy_ids, new_ids)
