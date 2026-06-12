from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status

from job_hunting.models import Company, JobPost, Resume, Scrape


User = get_user_model()


class TestSparseFieldsetsScrape(TestCase):
    """JSON:API `fields[<type>]` opt-in attribute filter on the scrape list.

    The list endpoint used to return every Scrape attribute on every row
    — including `job_content`, `html`, and `apply_candidates`, which can be
    tens to hundreds of KB each. With ~250 rows that ballooned the
    response past 5MB. Sparse fieldsets let the frontend ask for only the
    columns it actually renders.
    """

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="sf", password="pw")
        self.client.force_authenticate(user=self.user)
        self.company = Company.objects.create(name="Acme")
        self.post = JobPost.objects.create(
            title="Eng", company=self.company, created_by=self.user
        )
        Scrape.objects.create(
            url="https://acme.test/jobs/1",
            job_post=self.post,
            created_by=self.user,
            status="completed",
            job_content="A" * 10000,  # would dominate response without filter
            html="<html>" + "B" * 10000 + "</html>",
        )

    def test_no_filter_returns_all_attributes(self):
        resp = self.client.get("/api/v1/scrapes/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        attrs = resp.json()["data"][0]["attributes"]
        self.assertIn("url", attrs)
        self.assertIn("status", attrs)
        self.assertIn("job_content", attrs)
        self.assertIn("html", attrs)

    def test_fields_filter_drops_unrequested_attributes(self):
        resp = self.client.get("/api/v1/scrapes/?fields[scrape]=url,status")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        attrs = resp.json()["data"][0]["attributes"]
        self.assertEqual(set(attrs.keys()), {"url", "status"})
        # The heavy fields must NOT be present — that's the whole point.
        self.assertNotIn("job_content", attrs)
        self.assertNotIn("html", attrs)

    def test_fields_filter_ignores_unknown_attributes(self):
        # Garbage in fields[scrape] should be silently dropped, not 500.
        resp = self.client.get(
            "/api/v1/scrapes/?fields[scrape]=url,not_a_real_attr"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        attrs = resp.json()["data"][0]["attributes"]
        self.assertEqual(set(attrs.keys()), {"url"})

    def test_fields_filter_applies_to_retrieve(self):
        scrape_id = Scrape.objects.first().id
        resp = self.client.get(
            f"/api/v1/scrapes/{scrape_id}/?fields[scrape]=status"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        attrs = resp.json()["data"]["attributes"]
        self.assertEqual(set(attrs.keys()), {"status"})


class TestSparseFieldsetsResume(TestCase):
    """Resume serializer overrides to_resource() to inject a `summary`
    attribute computed from active_summary_content(). It must respect
    fields[resume] just like declared attributes."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="rf", password="pw")
        self.client.force_authenticate(user=self.user)
        self.resume = Resume.objects.create(
            user=self.user, title="Main", name="main"
        )

    def test_summary_omitted_when_not_in_fields(self):
        resp = self.client.get(
            f"/api/v1/resumes/{self.resume.id}/?fields[resume]=name,title"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        attrs = resp.json()["data"]["attributes"]
        self.assertEqual(set(attrs.keys()), {"name", "title"})
        self.assertNotIn("summary", attrs)

    def test_summary_emitted_when_in_fields(self):
        resp = self.client.get(
            f"/api/v1/resumes/{self.resume.id}/?fields[resume]=name,summary"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        attrs = resp.json()["data"]["attributes"]
        self.assertIn("summary", attrs)
        self.assertIn("name", attrs)
        self.assertNotIn("title", attrs)

    def test_summary_default_present_without_filter(self):
        resp = self.client.get(f"/api/v1/resumes/{self.resume.id}/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        attrs = resp.json()["data"]["attributes"]
        self.assertIn("summary", attrs)
        self.assertIn("title", attrs)


class TestSlimAliasEquivalence(TestCase):
    """`?slim=true` is being retired in favor of `?fields[<type>]=...`.

    For the deprecation window, the slim flag is internally aliased
    to the equivalent sparse-fieldset emission via
    BaseSerializer.slim_attributes. These tests pin the equivalence so
    a frontend caller can migrate one route at a time without observing
    a shape change.
    """

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="alias", password="pw")
        self.client.force_authenticate(user=self.user)
        # Resume's slim_attributes = [name, title, notes, favorite, profession]
        self.resume = Resume.objects.create(
            user=self.user,
            title="Aliased",
            name="aliased.docx",
            notes="ok",
            favorite=True,
            profession="Eng",
        )

    def _attrs(self, qs):
        resp = self.client.get(f"/api/v1/resumes/{qs}")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        return resp.json()["data"][0]["attributes"]

    def test_slim_and_fields_attribute_sets_match(self):
        # The two queries must surface the same attribute keys, because
        # the slim alias is just shorthand for the fieldset request.
        slim_attrs = self._attrs("?slim=true")
        explicit_attrs = self._attrs(
            "?fields[resume]=name,title,notes,favorite,profession"
        )
        self.assertEqual(set(slim_attrs.keys()), set(explicit_attrs.keys()))
        # And the values are identical for primitive fields.
        for k in slim_attrs:
            self.assertEqual(slim_attrs[k], explicit_attrs[k])

    def test_slim_attribute_set_is_exactly_slim_attributes(self):
        attrs = self._attrs("?slim=true")
        # Documented contract: slim_attributes — file_path / user_id /
        # status / section_order / effective_section_order / summary
        # must be absent.
        self.assertEqual(
            set(attrs.keys()),
            {"name", "title", "notes", "favorite", "profession"},
        )

    def test_slim_emits_meta_counts(self):
        # Resume's legacy slim side-channel: per-resource meta.counts.
        # Frontend models depend on the count fields for badge UIs.
        resp = self.client.get("/api/v1/resumes/?slim=true")
        meta = resp.json()["data"][0]["meta"]
        for k in (
            "job_application_count", "score_count",
            "experience_count", "skill_count",
        ):
            self.assertIn(k, meta)

    def test_meta_counts_param_is_independent_of_slim(self):
        # Forward path: callers ask for counts explicitly without
        # the slim flag. fields[<type>] still narrows attributes;
        # meta=counts is orthogonal.
        resp = self.client.get(
            "/api/v1/resumes/?fields[resume]=name&meta=counts"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        record = resp.json()["data"][0]
        self.assertEqual(set(record["attributes"].keys()), {"name"})
        self.assertIn("meta", record)
        self.assertIn("job_application_count", record["meta"])

    def test_meta_counts_omitted_by_default(self):
        # Without slim and without meta=counts, the meta block stays off.
        resp = self.client.get("/api/v1/resumes/")
        record = resp.json()["data"][0]
        self.assertNotIn("meta", record)

    def test_slim_short_circuits_included_sideloads(self):
        # Legacy slim contract held: list responses with ?slim=true
        # never carry a top-level `included[]`.
        body = self.client.get("/api/v1/resumes/?slim=true").json()
        self.assertNotIn("included", body)


class TestSlimDeprecationLog(TestCase):
    """`_is_slim_request` emits a structured log line on every consumption
    so we can watch the migration close. Pin the format so log-scraping
    dashboards don't silently break."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="logged", password="pw")
        self.client.force_authenticate(user=self.user)
        Resume.objects.create(user=self.user, title="t")

    def test_slim_request_logs_deprecation_line(self):
        with self.assertLogs("job_hunting.api.views.base", level="INFO") as ctx:
            self.client.get("/api/v1/resumes/?slim=true")
        joined = "\n".join(ctx.output)
        self.assertIn("serializer.slim.deprecated", joined)
        self.assertIn("/api/v1/resumes/", joined)

    def test_non_slim_request_does_not_log(self):
        # Guard against a future regression where the gate falls open
        # and we log on every list request.
        with self.assertNoLogs("job_hunting.api.views.base", level="INFO"):
            self.client.get("/api/v1/resumes/")


class TestSparseFieldsetsUser(TestCase):
    """`fields[user]=...` on /me/ — broadening sparse-fieldset coverage
    beyond Scrape/Resume so the deprecation of slim across all serializers
    has a regression backstop on the User serializer too."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="me", password="pw", email="me@x", first_name="Me",
        )
        self.client.force_authenticate(user=self.user)

    def test_fields_user_narrows_attributes(self):
        resp = self.client.get("/api/v1/me/?fields[user]=username,email")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        attrs = resp.json()["data"]["attributes"]
        self.assertEqual({"username", "email"}, set(attrs.keys()))

    def test_fields_user_unknown_attr_silently_dropped(self):
        resp = self.client.get("/api/v1/me/?fields[user]=username,not_a_thing")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            set(resp.json()["data"]["attributes"].keys()), {"username"},
        )
