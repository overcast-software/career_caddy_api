from io import BytesIO
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status
from openpyxl import load_workbook
from job_hunting.models import Company, JobPost, JobApplication, Question, Answer

User = get_user_model()


class TestCareerDataExport(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_superuser(
            username="exportuser", password="pass", email="e@x.com"
        )
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Acme")
        self.jp = JobPost.objects.create(
            title="Engineer", company=self.company, link="https://acme.com/1",
            created_by=self.user,
        )
        self.ja = JobApplication.objects.create(
            user=self.user, job_post=self.jp, company=self.company, status="Applied",
        )
        self.q = Question.objects.create(
            application=self.ja, company=self.company, created_by=self.user,
            content="Why Acme?", favorite=True,
        )
        self.a = Answer.objects.create(
            question=self.q, content="Great culture", favorite=False, status="draft",
        )

    def test_export_returns_xlsx(self):
        resp = self.client.get("/api/v1/career-data/export/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("spreadsheetml", resp["Content-Type"])
        self.assertIn("attachment", resp["Content-Disposition"])

    def test_export_contains_all_sheets(self):
        resp = self.client.get("/api/v1/career-data/export/")
        wb = load_workbook(BytesIO(resp.content))
        self.assertEqual(set(wb.sheetnames), {"job-posts", "job-applications", "questions", "answers"})

    def test_export_data_integrity(self):
        resp = self.client.get("/api/v1/career-data/export/")
        wb = load_workbook(BytesIO(resp.content))
        # job-posts: header + 1 row
        jp_rows = list(wb["job-posts"].iter_rows(values_only=True))
        self.assertEqual(len(jp_rows), 2)
        self.assertEqual(jp_rows[1][1], "Engineer")  # title column
        # questions
        q_rows = list(wb["questions"].iter_rows(values_only=True))
        self.assertEqual(len(q_rows), 2)
        self.assertEqual(q_rows[1][4], "Why Acme?")  # content column

    def test_export_requires_auth(self):
        anon = APIClient()
        resp = anon.get("/api/v1/career-data/export/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class TestCareerDataImport(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.superuser = User.objects.create_superuser(
            username="importsuper", password="pass", email="s@x.com"
        )
        self.regular = User.objects.create_user(username="importreg", password="pass")
        self.client.force_authenticate(user=self.superuser)

    def _make_xlsx(self):
        """Build a minimal xlsx in memory with one record per sheet."""
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "job-posts"
        ws.append(["id", "title", "company", "description", "link",
                    "posted_date", "extraction_date", "salary_min", "salary_max",
                    "location", "remote", "created_at"])
        ws.append([1, "Dev", "NewCo", "Build stuff", "https://newco.com/dev",
                    None, None, None, None, "Remote", True, None])

        ws2 = wb.create_sheet("job-applications")
        ws2.append(["id", "job_post_id", "company", "status",
                     "applied_at", "tracking_url", "notes"])
        ws2.append([1, 1, "NewCo", "Applied", None, None, "Great fit"])

        ws3 = wb.create_sheet("questions")
        ws3.append(["id", "application_id", "company", "job_post_id", "content", "favorite", "created_at"])
        ws3.append([1, 1, "NewCo", 1, "Tell me about yourself", True, None])

        ws4 = wb.create_sheet("answers")
        ws4.append(["id", "question_id", "content", "favorite", "status", "created_at"])
        ws4.append([1, 1, "I'm a developer", False, "final", None])

        buf = BytesIO()
        wb.save(buf)
        buf.seek(0)
        buf.name = "test.xlsx"
        return buf

    def test_import_creates_records(self):
        f = self._make_xlsx()
        resp = self.client.post("/api/v1/career-data/import/", {"file": f}, format="multipart")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()["data"]
        self.assertEqual(data["job-posts"]["created"], 1)
        self.assertEqual(data["job-applications"]["created"], 1)
        self.assertEqual(data["questions"]["created"], 1)
        self.assertEqual(data["answers"]["created"], 1)
        self.assertEqual(JobPost.objects.count(), 1)
        self.assertEqual(Question.objects.count(), 1)

    def test_import_records_discovery_for_caller(self):
        """CSV import must record JobPostDiscovery for the importer.
        Without this, imported posts have zero per-user signals and
        are invisible until the user manually triggers another action."""
        from job_hunting.models import JobPostDiscovery

        f = self._make_xlsx()
        self.client.post("/api/v1/career-data/import/", {"file": f}, format="multipart")
        jp = JobPost.objects.get(link="https://newco.com/dev")
        disc = JobPostDiscovery.objects.filter(
            job_post=jp, user=self.superuser
        ).first()
        self.assertIsNotNone(disc, "import must record discovery for caller")
        self.assertEqual(disc.source, "import")

    def test_import_dedupe_merges_empty_company(self):
        """A pre-existing JobPost with the same link but NULL company
        gets its company filled from the imported row. Same merge policy
        as the create() endpoint."""
        from job_hunting.models import JobPostDiscovery

        existing = JobPost.objects.create(
            title="Stub",
            company=None,
            link="https://newco.com/dev",
            description="x" * 500,
            created_by=self.regular,
        )
        f = self._make_xlsx()
        self.client.post("/api/v1/career-data/import/", {"file": f}, format="multipart")
        existing.refresh_from_db()
        self.assertIsNotNone(
            existing.company_id,
            "import must merge empty company_id onto existing duplicate",
        )
        self.assertEqual(existing.company.name, "NewCo")
        # And the importer gets a discovery row even on the dedupe path.
        self.assertTrue(
            JobPostDiscovery.objects.filter(
                job_post=existing, user=self.superuser, source="import"
            ).exists()
        )

    def test_import_skips_duplicates(self):
        f1 = self._make_xlsx()
        self.client.post("/api/v1/career-data/import/", {"file": f1}, format="multipart")
        f2 = self._make_xlsx()
        resp = self.client.post("/api/v1/career-data/import/", {"file": f2}, format="multipart")
        data = resp.json()["data"]
        self.assertEqual(data["job-posts"]["skipped"], 1)
        self.assertEqual(data["job-posts"]["created"], 0)
        self.assertEqual(data["job-applications"]["skipped"], 1)
        self.assertEqual(data["questions"]["skipped"], 1)
        self.assertEqual(data["answers"]["skipped"], 1)
        self.assertEqual(JobPost.objects.count(), 1)

    def test_import_skips_duplicates_without_link(self):
        """Job posts without a link should still be deduplicated by title+company+user."""
        from openpyxl import Workbook

        def _make_no_link_xlsx():
            wb = Workbook()
            ws = wb.active
            ws.title = "job-posts"
            ws.append(["id", "title", "company", "description", "link",
                        "posted_date", "extraction_date", "salary_min", "salary_max",
                        "location", "remote", "created_at"])
            ws.append([1, "Manual Entry", "SomeCo", "No link job", None,
                        None, None, None, None, "Remote", True, None])

            ws2 = wb.create_sheet("job-applications")
            ws2.append(["id", "job_post_id", "company", "status",
                         "applied_at", "tracking_url", "notes"])
            ws2.append([1, 1, "SomeCo", "Applied", None, None, "Notes"])

            ws3 = wb.create_sheet("questions")
            ws3.append(["id", "application_id", "company", "job_post_id",
                         "content", "favorite", "created_at"])
            ws3.append([1, 1, "SomeCo", 1, "Why here?", False, None])

            ws4 = wb.create_sheet("answers")
            ws4.append(["id", "question_id", "content", "favorite", "status", "created_at"])
            ws4.append([1, 1, "Because reasons", False, "draft", None])

            buf = BytesIO()
            wb.save(buf)
            buf.seek(0)
            buf.name = "nolink.xlsx"
            return buf

        # First import
        f1 = _make_no_link_xlsx()
        resp1 = self.client.post("/api/v1/career-data/import/", {"file": f1}, format="multipart")
        self.assertEqual(resp1.json()["data"]["job-posts"]["created"], 1)
        self.assertEqual(JobPost.objects.count(), 1)

        # Second import — should skip everything
        f2 = _make_no_link_xlsx()
        resp2 = self.client.post("/api/v1/career-data/import/", {"file": f2}, format="multipart")
        data = resp2.json()["data"]
        self.assertEqual(data["job-posts"]["skipped"], 1)
        self.assertEqual(data["job-posts"]["created"], 0)
        self.assertEqual(data["job-applications"]["skipped"], 1)
        self.assertEqual(data["questions"]["skipped"], 1)
        self.assertEqual(data["answers"]["skipped"], 1)
        self.assertEqual(JobPost.objects.count(), 1)

    def test_import_superuser_only(self):
        self.client.force_authenticate(user=self.regular)
        f = self._make_xlsx()
        resp = self.client.post("/api/v1/career-data/import/", {"file": f}, format="multipart")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_import_no_file_400(self):
        resp = self.client.post("/api/v1/career-data/import/", {}, format="multipart")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_import_bad_file_400(self):
        bad = BytesIO(b"not an xlsx")
        bad.name = "bad.xlsx"
        resp = self.client.post("/api/v1/career-data/import/", {"file": bad}, format="multipart")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class TestExportImportRoundTrip(TestCase):
    """Export data, clear DB, import it back — verify integrity."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_superuser(
            username="roundtrip", password="pass", email="r@x.com"
        )
        self.client.force_authenticate(user=self.user)

    def test_round_trip(self):
        co = Company.objects.create(name="RoundCo")
        jp = JobPost.objects.create(
            title="RoundDev", company=co, link="https://round.co/1", created_by=self.user,
        )
        ja = JobApplication.objects.create(
            user=self.user, job_post=jp, company=co, status="Interviewing",
        )
        q = Question.objects.create(
            application=ja, company=co, created_by=self.user,
            content="Round question?", favorite=True,
        )
        Answer.objects.create(question=q, content="Round answer", favorite=True, status="final")

        # Export
        resp = self.client.get("/api/v1/career-data/export/")
        xlsx_bytes = resp.content

        # Clear
        Answer.objects.all().delete()
        Question.objects.all().delete()
        JobApplication.objects.all().delete()
        JobPost.objects.all().delete()
        self.assertEqual(JobPost.objects.count(), 0)

        # Import
        f = BytesIO(xlsx_bytes)
        f.name = "roundtrip.xlsx"
        resp = self.client.post("/api/v1/career-data/import/", {"file": f}, format="multipart")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()["data"]
        self.assertEqual(data["job-posts"]["created"], 1)
        self.assertEqual(data["job-applications"]["created"], 1)
        self.assertEqual(data["questions"]["created"], 1)
        self.assertEqual(data["answers"]["created"], 1)

        # Verify
        self.assertEqual(JobPost.objects.first().title, "RoundDev")
        self.assertEqual(Question.objects.first().content, "Round question?")
        self.assertTrue(Answer.objects.first().favorite)
