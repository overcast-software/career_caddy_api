from decimal import Decimal

from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import AiUsage
from job_hunting.lib.pricing import estimate_cost

User = get_user_model()


class TestPricing(TestCase):
    def test_known_model(self):
        cost = estimate_cost("openai:gpt-4o-mini", 1_000_000, 1_000_000)
        # 0.15 input + 0.60 output = 0.75
        self.assertEqual(cost, Decimal("0.750000"))

    def test_unknown_model_zero_cost(self):
        cost = estimate_cost("ollama:llama3", 500_000, 500_000)
        self.assertEqual(cost, Decimal("0.000000"))

    def test_zero_tokens(self):
        cost = estimate_cost("openai:gpt-4o-mini", 0, 0)
        self.assertEqual(cost, Decimal("0.000000"))

    def test_small_token_count(self):
        # 1000 input tokens on gpt-4o-mini: 0.15 * 1000 / 1M = 0.000150
        cost = estimate_cost("openai:gpt-4o-mini", 1000, 0)
        self.assertEqual(cost, Decimal("0.000150"))


class TestAiUsageModel(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")

    def test_create_record(self):
        obj = AiUsage.objects.create(
            user=self.user,
            agent_name="career_caddy_chat",
            model_name="openai:gpt-4o-mini",
            trigger="chat",
            request_tokens=500,
            response_tokens=200,
            total_tokens=700,
        )
        self.assertIsNotNone(obj.id)
        self.assertEqual(obj.agent_name, "career_caddy_chat")
        self.assertIsNotNone(obj.created_at)

    def test_defaults(self):
        obj = AiUsage.objects.create(user=self.user, agent_name="test", model_name="test", trigger="test")
        self.assertEqual(obj.request_tokens, 0)
        self.assertEqual(obj.response_tokens, 0)
        self.assertEqual(obj.total_tokens, 0)
        self.assertEqual(obj.request_count, 1)
        self.assertEqual(obj.estimated_cost_usd, Decimal("0"))


class TestAiUsageAPI(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="testuser", password="pass")
        self.client.force_authenticate(user=self.user)

    def _create_record(self, **overrides):
        defaults = {
            "user": self.user,
            "agent_name": "career_caddy_chat",
            "model_name": "openai:gpt-4o-mini",
            "trigger": "chat",
            "request_tokens": 500,
            "response_tokens": 200,
            "total_tokens": 700,
            "estimated_cost_usd": Decimal("0.000195"),
        }
        defaults.update(overrides)
        return AiUsage.objects.create(**defaults)

    # -- Create --

    def test_create_jsonapi(self):
        payload = {
            "data": {
                "type": "ai-usage",
                "attributes": {
                    "agent_name": "career_caddy_chat",
                    "model_name": "openai:gpt-4o-mini",
                    "trigger": "chat",
                    "request_tokens": 1000,
                    "response_tokens": 500,
                    "total_tokens": 1500,
                },
            }
        }
        resp = self.client.post("/api/v1/ai-usages/", data=payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        data = resp.json()["data"]
        self.assertEqual(data["attributes"]["agent_name"], "career_caddy_chat")
        self.assertEqual(data["attributes"]["model_name"], "openai:gpt-4o-mini")
        self.assertEqual(data["attributes"]["request_tokens"], 1000)
        self.assertEqual(data["attributes"]["response_tokens"], 500)
        # Cost should be computed server-side
        cost = Decimal(data["attributes"]["estimated_cost_usd"])
        self.assertGreater(cost, Decimal("0"))

    def test_create_computes_cost(self):
        payload = {
            "data": {
                "type": "ai-usage",
                "attributes": {
                    "agent_name": "test",
                    "model_name": "openai:gpt-4o-mini",
                    "trigger": "pipeline",
                    "request_tokens": 1_000_000,
                    "response_tokens": 1_000_000,
                    "total_tokens": 2_000_000,
                },
            }
        }
        resp = self.client.post("/api/v1/ai-usages/", data=payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        cost = Decimal(resp.json()["data"]["attributes"]["estimated_cost_usd"])
        self.assertEqual(cost, Decimal("0.750000"))

    def test_create_unknown_model_zero_cost(self):
        payload = {
            "data": {
                "type": "ai-usage",
                "attributes": {
                    "agent_name": "test",
                    "model_name": "ollama:phi3",
                    "trigger": "pipeline",
                    "request_tokens": 5000,
                    "response_tokens": 5000,
                    "total_tokens": 10000,
                },
            }
        }
        resp = self.client.post("/api/v1/ai-usages/", data=payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        cost = Decimal(resp.json()["data"]["attributes"]["estimated_cost_usd"])
        self.assertEqual(cost, Decimal("0.000000"))

    def test_create_sets_user_from_auth(self):
        payload = {
            "data": {
                "type": "ai-usage",
                "attributes": {
                    "agent_name": "test",
                    "model_name": "test",
                    "trigger": "chat",
                    "request_tokens": 100,
                    "response_tokens": 50,
                    "total_tokens": 150,
                },
            }
        }
        resp = self.client.post("/api/v1/ai-usages/", data=payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        obj = AiUsage.objects.get(pk=resp.json()["data"]["id"])
        self.assertEqual(obj.user_id, self.user.id)

    # -- List --

    def test_list(self):
        self._create_record()
        self._create_record(trigger="pipeline")
        resp = self.client.get("/api/v1/ai-usages/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()["meta"]["total"], 2)

    def test_list_filter_by_trigger(self):
        self._create_record(trigger="chat")
        self._create_record(trigger="pipeline")
        resp = self.client.get("/api/v1/ai-usages/?trigger=pipeline")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()["meta"]["total"], 1)

    def test_list_filter_by_agent_name(self):
        self._create_record(agent_name="career_caddy_chat")
        self._create_record(agent_name="job_extractor")
        resp = self.client.get("/api/v1/ai-usages/?agent_name=job_extractor")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()["meta"]["total"], 1)

    # -- User isolation --

    def test_user_sees_only_own_records(self):
        other = User.objects.create_user(username="other", password="pass")
        self._create_record()
        AiUsage.objects.create(
            user=other, agent_name="test", model_name="test", trigger="chat",
        )
        resp = self.client.get("/api/v1/ai-usages/")
        self.assertEqual(resp.json()["meta"]["total"], 1)

    def test_staff_sees_all_records(self):
        staff = User.objects.create_user(username="admin", password="pass", is_staff=True)
        other = User.objects.create_user(username="other", password="pass")
        self._create_record()
        AiUsage.objects.create(
            user=other, agent_name="test", model_name="test", trigger="chat",
        )
        self.client.force_authenticate(user=staff)
        resp = self.client.get("/api/v1/ai-usages/")
        self.assertEqual(resp.json()["meta"]["total"], 2)

    def test_staff_filter_by_user(self):
        staff = User.objects.create_user(username="admin", password="pass", is_staff=True)
        other = User.objects.create_user(username="other", password="pass")
        self._create_record()
        AiUsage.objects.create(
            user=other, agent_name="test", model_name="test", trigger="chat",
        )
        self.client.force_authenticate(user=staff)
        resp = self.client.get(f"/api/v1/ai-usages/?user_id={other.id}")
        self.assertEqual(resp.json()["meta"]["total"], 1)

    # -- Summary --

    def test_summary_endpoint(self):
        self._create_record(request_tokens=1000, response_tokens=500, total_tokens=1500)
        self._create_record(request_tokens=2000, response_tokens=1000, total_tokens=3000)
        resp = self.client.get("/api/v1/ai-usages/summary/?period=daily&days=7")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()["data"]
        self.assertIn("buckets", body)
        self.assertIn("totals", body)
        self.assertEqual(body["totals"]["total_tokens"], 4500)
        self.assertEqual(body["totals"]["request_count"], 2)

    def test_summary_group_by_agent(self):
        self._create_record(agent_name="chat_agent")
        self._create_record(agent_name="extractor")
        resp = self.client.get("/api/v1/ai-usages/summary/?period=daily&days=7&group_by=agent_name")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        buckets = resp.json()["data"]["buckets"]
        agent_names = {b["agent_name"] for b in buckets}
        self.assertEqual(agent_names, {"chat_agent", "extractor"})

    def test_summary_group_by_model(self):
        self._create_record(model_name="openai:gpt-4o-mini")
        self._create_record(model_name="openai:gpt-4o")
        resp = self.client.get("/api/v1/ai-usages/summary/?period=daily&days=7&group_by=model_name")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        buckets = resp.json()["data"]["buckets"]
        models = {b["model_name"] for b in buckets}
        self.assertEqual(models, {"openai:gpt-4o-mini", "openai:gpt-4o"})

    # -- Auth --

    def test_unauthenticated_rejected(self):
        self.client.force_authenticate(user=None)
        resp = self.client.get("/api/v1/ai-usages/")
        self.assertIn(resp.status_code, [401, 403])
