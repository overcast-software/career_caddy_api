"""POST /api/v1/scrapes/ is staff-only during alpha.

Non-staff users initiating scrapes get 403. The intended creation paths
for non-staff are the inbox / extension capture flows that mint JobPost
shells without scraping. This is the temporary alpha crutch; multi-tenant
resource isolation is the long-term replacement (see notes.org Pending
Approval).
"""
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient


User = get_user_model()


class ScrapeCreateStaffGateTests(TestCase):
    def _post(self, client):
        body = {
            "data": {"attributes": {"url": "https://example.com/jobs/1", "status": "hold"}}
        }
        return client.post("/api/v1/scrapes/", body, format="json")

    def test_non_staff_user_receives_403(self):
        user = User.objects.create_user(
            username="non_staff", password="p", is_staff=False
        )
        client = APIClient()
        client.force_authenticate(user=user)

        resp = self._post(client)

        self.assertEqual(resp.status_code, 403)
        body = resp.json()
        self.assertEqual(
            body["errors"][0]["detail"], "Scraping is staff-only during alpha."
        )

    def test_staff_user_passes_the_gate(self):
        # Staff users still hit the rest of the create flow. We don't assert
        # 201 here because downstream validation (URL policy, dedup, etc.)
        # might reject for unrelated reasons; the assertion is just "not 403".
        user = User.objects.create_user(
            username="staff", password="p", is_staff=True
        )
        client = APIClient()
        client.force_authenticate(user=user)

        resp = self._post(client)

        self.assertNotEqual(resp.status_code, 403)
