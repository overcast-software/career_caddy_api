from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from job_hunting.models import JobPost, Scrape
from job_hunting.models.scrape_status import ScrapeStatus


User = get_user_model()


class PersistExtractionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="doug", password="p")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.scrape = Scrape.objects.create(
            url="https://example.com/job/1",
            job_content="Senior Engineer at Acme — building stuff.",
            status="extracting",
            created_by=self.user,
        )

    def test_creates_job_post(self):
        resp = self.client.post(
            f"/api/v1/scrapes/{self.scrape.id}/persist-extraction/",
            {
                "attributes": {
                    "title": "Senior Engineer",
                    "company_name": "Acme Corp",
                    "description": "Build things with care.",
                }
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["meta"]["persisted"])
        jp_id = body["meta"]["job_post_id"]
        self.assertIsNotNone(jp_id)
        jp = JobPost.objects.get(pk=jp_id)
        self.assertEqual(jp.title, "Senior Engineer")

    def test_non_owner_rejected(self):
        other = User.objects.create_user(username="other", password="p")
        self.client.force_authenticate(user=other)
        resp = self.client.post(
            f"/api/v1/scrapes/{self.scrape.id}/persist-extraction/",
            {"attributes": {"title": "X", "company_name": "Y"}},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_invalid_parsed_data_400s(self):
        resp = self.client.post(
            f"/api/v1/scrapes/{self.scrape.id}/persist-extraction/",
            {"attributes": {"title": ""}},  # empty title fails validator
            format="json",
        )
        self.assertEqual(resp.status_code, 400)


class GraphTransitionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="doug", password="p")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.scrape = Scrape.objects.create(
            url="https://example.com/job/1",
            status="pending",
            created_by=self.user,
        )

    def test_records_scrape_status_row(self):
        resp = self.client.post(
            f"/api/v1/scrapes/{self.scrape.id}/graph-transition/",
            {
                "graph_node": "Tier1Mini",
                "graph_payload": {
                    "routed_to": "EvaluateExtraction",
                    "tokens": 1234,
                    "cost_usd": 0.0012,
                },
                "note": "first extraction attempt",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        row = ScrapeStatus.objects.get(scrape=self.scrape, graph_node="Tier1Mini")
        self.assertEqual(row.graph_payload["routed_to"], "EvaluateExtraction")
        self.assertEqual(row.note, "first extraction attempt")

    def test_missing_graph_node_400s(self):
        resp = self.client.post(
            f"/api/v1/scrapes/{self.scrape.id}/graph-transition/",
            {"graph_payload": {}},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_graph_trace_walks_source_scrape_chain(self):
        child = Scrape.objects.create(
            url="https://ats.example/apply/1",
            status="completed",
            created_by=self.user,
            source_scrape=self.scrape,
            source="redirect",
        )
        # One transition on parent, one on child
        self.client.post(
            f"/api/v1/scrapes/{self.scrape.id}/graph-transition/",
            {"graph_node": "ResolveFinalUrl", "graph_payload": {"routed_to": "CheckLinkDedup"}},
            format="json",
        )
        self.client.post(
            f"/api/v1/scrapes/{child.id}/graph-transition/",
            {"graph_node": "CheckLinkDedup", "graph_payload": {"routed_to": "WaitReadySelector"}},
            format="json",
        )
        resp = self.client.get(f"/api/v1/scrapes/{child.id}/graph-trace/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        chain_ids = [c["id"] for c in body["meta"]["chain"]]
        self.assertEqual(chain_ids, [self.scrape.id, child.id])
        nodes = [row["graph_node"] for row in body["data"]]
        self.assertIn("ResolveFinalUrl", nodes)
        self.assertIn("CheckLinkDedup", nodes)


class GraphAdminEndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="doug", password="p")
        self.staff = User.objects.create_user(
            username="admin", password="p", is_staff=True
        )
        self.client = APIClient()

    def test_structure_requires_staff(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.get("/api/v1/admin/graph-structure/")
        self.assertEqual(resp.status_code, 403)

    def test_structure_returns_nodes_and_edges(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.get("/api/v1/admin/graph-structure/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        node_ids = {n["id"] for n in data["nodes"]}
        self.assertIn("Tier0CSS", node_ids)
        self.assertIn("ObstacleAgent", node_ids)
        self.assertIn("ResolveFinalUrl", node_ids)
        self.assertTrue(any(
            e["from"] == "DetectObstacle" and e["to"] == "ObstacleAgent"
            for e in data["edges"]
        ))

    def test_aggregate_empty_ok(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.get("/api/v1/admin/graph-aggregate/?since=1d")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("edges", resp.json()["data"])

    def test_mermaid_json(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.get("/api/v1/admin/graph-mermaid/")
        self.assertEqual(resp.status_code, 200)
        src = resp.json()["data"]["mermaid"]
        self.assertIn("stateDiagram-v2", src)
        self.assertIn("StartScrape", src)
        self.assertIn("ResolveFinalUrl --> CheckLinkDedup", src)
        self.assertIn("DuplicateShortCircuit --> [*]", src)

    def test_mermaid_text(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.get("/api/v1/admin/graph-mermaid/?as=text")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp["Content-Type"].startswith("text/plain"))
        self.assertIn(b"stateDiagram-v2", resp.content)

    def test_mermaid_non_staff_forbidden(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.get("/api/v1/admin/graph-mermaid/")
        self.assertEqual(resp.status_code, 403)


class UpdateFromOutcomeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="doug", password="p")
        self.staff = User.objects.create_user(
            username="admin", password="p", is_staff=True
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff)
        self.scrape = Scrape.objects.create(
            url="https://example.com/job/1",
            status="completed",
            created_by=self.user,
        )

    def test_records_profile_outcome(self):
        resp = self.client.post(
            "/api/v1/scrape-profiles/example.com/update-from-outcome/",
            {"scrape_id": self.scrape.id, "success": True, "tier0_hit": True},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["data"]["recorded"])

    def test_requires_scrape_id(self):
        resp = self.client.post(
            "/api/v1/scrape-profiles/example.com/update-from-outcome/",
            {"success": True},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
