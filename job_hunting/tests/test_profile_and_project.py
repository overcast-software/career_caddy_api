from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase
from rest_framework import status
from job_hunting.lib.db import init_sqlalchemy
from job_hunting.lib.models.base import BaseModel, Base
from job_hunting.models import Profile
from job_hunting.models import Project


class ProfileAPITests(APITestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Initialize SQLAlchemy schema
        init_sqlalchemy()

        cls.session = BaseModel.get_session()
        cls.engine = cls.session.bind
        Base.metadata.create_all(bind=cls.engine)
        # Create Django user
        User = get_user_model()
        cls.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123",
            first_name="Test",
            last_name="User",
        )

        # Create Django Profile
        cls.profile = Profile.objects.create(user_id=cls.user.id, phone="555-123-4567")

    def get_jwt_token(self):
        """Helper to obtain JWT token for authenticated requests"""
        response = self.client.post(
            "/api/v1/token/", {"username": "testuser", "password": "testpass123"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data["access"]

    def test_profile_authenticated_returns_user_resource(self):
        """Test that authenticated GET /api/v1/profile/ returns user resource with phone"""
        token = self.get_jwt_token()
        response = self.client.get(
            "/api/v1/profile/", HTTP_AUTHORIZATION=f"Bearer {token}"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertEqual(data["type"], "user")
        self.assertEqual(data["id"], str(self.user.id))

        attributes = data["attributes"]
        self.assertEqual(attributes["username"], "testuser")
        self.assertEqual(attributes["email"], "test@example.com")
        self.assertEqual(attributes["first_name"], "Test")
        self.assertEqual(attributes["last_name"], "User")
        self.assertEqual(attributes["phone"], "555-123-4567")

    def test_profile_unauthenticated_fails(self):
        """Test that unauthenticated GET /api/v1/profile/ returns 401"""
        response = self.client.get("/api/v1/profile/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_profile_missing_sa_profile_phone_empty(self):
        """Test that missing SA Profile still returns valid user with empty phone"""
        # Create a new user without SA Profile
        User = get_user_model()
        user2 = User.objects.create_user(
            username="testuser2", email="test2@example.com", password="testpass123"
        )

        # Get token for this user
        response = self.client.post(
            "/api/v1/token/", {"username": "testuser2", "password": "testpass123"}
        )
        token = response.data["access"]

        # Request profile
        response = self.client.get(
            "/api/v1/profile/", HTTP_AUTHORIZATION=f"Bearer {token}"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertEqual(data["type"], "user")
        self.assertEqual(data["id"], str(user2.id))

        attributes = data["attributes"]
        self.assertEqual(attributes["phone"], "")  # Should be empty string


class ProjectModelTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Initialize SQLAlchemy schema
        init_sqlalchemy()
        cls.session = BaseModel.get_session()
        cls.engine = cls.session.bind

    def test_project_model_roundtrip_simple(self):
        """Test simple Project model round-trip using explicit fields"""
        User = get_user_model()
        user = User.objects.create_user(username="projuser1", password="pass")
        project = Project.objects.create(user_id=user.id, title="Test Project")
        self.assertIsNotNone(project.id)

        retrieved = Project.objects.filter(pk=project.id).first()
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.id, project.id)
        self.assertEqual(retrieved.title, "Test Project")
        self.assertEqual(retrieved.user_id, user.id)

    def test_project_model_explicit_fields(self):
        """Test Project model with explicit known fields"""
        User = get_user_model()
        user = User.objects.create_user(username="projuser2", password="pass")

        project = Project.objects.create(user_id=user.id, title="test title")
        self.assertIsNotNone(project.id)

        # Verify retrieval
        retrieved = Project.objects.filter(pk=project.id).first()
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.id, project.id)
