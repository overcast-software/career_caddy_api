from datetime import timedelta
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status
from job_hunting.models import ApiKey

User = get_user_model()


class TestApiKeyModel(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")

    def test_generate_key(self):
        obj, raw_key = ApiKey.generate_key(name="test", user_id=self.user.id)
        self.assertIsNotNone(obj.id)
        self.assertTrue(raw_key.startswith("jh_"))
        self.assertEqual(obj.key_prefix, raw_key[:12])
        self.assertNotEqual(obj.key_hash, raw_key)

    def test_authenticate_valid(self):
        obj, raw_key = ApiKey.generate_key(name="test", user_id=self.user.id)
        found = ApiKey.authenticate(raw_key)
        self.assertIsNotNone(found)
        self.assertEqual(found.id, obj.id)

    def test_authenticate_wrong_key(self):
        ApiKey.generate_key(name="test", user_id=self.user.id)
        result = ApiKey.authenticate("jh_wrongkey")
        self.assertIsNone(result)

    def test_authenticate_expired(self):
        obj, raw_key = ApiKey.generate_key(name="test", user_id=self.user.id, expires_days=-1)
        result = ApiKey.authenticate(raw_key)
        self.assertIsNone(result)

    def test_authenticate_inactive(self):
        obj, raw_key = ApiKey.generate_key(name="test", user_id=self.user.id)
        obj.revoke()
        result = ApiKey.authenticate(raw_key)
        self.assertIsNone(result)

    def test_get_scopes(self):
        obj, _ = ApiKey.generate_key(name="test", user_id=self.user.id, scopes=["read", "write"])
        self.assertEqual(obj.get_scopes(), ["read", "write"])

    def test_get_scopes_empty(self):
        obj, _ = ApiKey.generate_key(name="test", user_id=self.user.id)
        self.assertEqual(obj.get_scopes(), [])

    def test_has_scope(self):
        obj, _ = ApiKey.generate_key(name="test", user_id=self.user.id, scopes=["read"])
        self.assertTrue(obj.has_scope("read"))
        self.assertFalse(obj.has_scope("write"))

    def test_has_scope_wildcard(self):
        obj, _ = ApiKey.generate_key(name="test", user_id=self.user.id, scopes=["*"])
        self.assertTrue(obj.has_scope("read"))
        self.assertTrue(obj.has_scope("write"))

    def test_revoke(self):
        obj, _ = ApiKey.generate_key(name="test", user_id=self.user.id)
        obj.revoke()
        obj.refresh_from_db()
        self.assertFalse(obj.is_active)


class TestApiKeyAPI(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="apiuser", password="pass")
        self.client.force_authenticate(user=self.user)

    def test_list_api_keys(self):
        ApiKey.generate_key(name="mykey", user_id=self.user.id)
        response = self.client.get("/api/v1/api-keys/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertIn("data", data)
        self.assertEqual(len(data["data"]), 1)

    def test_create_api_key(self):
        payload = {"data": {"type": "api-key", "attributes": {"name": "newkey"}}}
        response = self.client.post("/api/v1/api-keys/", data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.json()
        self.assertIn("key", data["data"]["attributes"])
        self.assertTrue(data["data"]["attributes"]["key"].startswith("jh_"))

    def test_revoke_api_key(self):
        obj, _ = ApiKey.generate_key(name="to-revoke", user_id=self.user.id)
        response = self.client.delete(f"/api/v1/api-keys/{obj.id}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        obj.refresh_from_db()
        self.assertFalse(obj.is_active)
