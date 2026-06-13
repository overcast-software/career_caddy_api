"""Tests for the Phase A self-FK alias feature on Company.

Covers:
- ``CheckConstraint company_canonical_not_self`` blocks a self-loop
  at the DB layer.
- ``Company.mark_as_alias_of`` service verb: basic case, chain
  flattening (A→B→C bookkeeping), cycle rejection, idempotency,
  self-target rejection.
- ``POST /api/v1/companies/:id/mark-as-alias-of/`` returns 403 for
  non-staff, 200 for staff, 400 on missing/invalid target.
- ``GET /api/v1/companies/:id/aliases/`` sub-collection returns
  Companies whose ``canonical_id`` equals the parent id.
- ``CompanySerializer`` emits ``relationships.aliases`` and
  ``relationships.canonical`` as links-only by default; with
  ``?include=aliases``/``?include=canonical`` it populates the
  ``data`` linkage + the top-level ``included[]``.
"""

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from job_hunting.models import Company

User = get_user_model()


class TestCompanyCanonicalCheckConstraint(TestCase):
    """The DB-level CheckConstraint blocks a self-loop write."""

    def test_self_canonical_trips_constraint(self):
        company = Company.objects.create(name="Acme Self")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Company.objects.filter(pk=company.id).update(
                    canonical_id=company.id
                )


class TestMarkAsAliasOf(TestCase):
    """Service verb on the model."""

    def test_basic_case_sets_canonical(self):
        target = Company.objects.create(name="Acme Corporation")
        source = Company.objects.create(name="Acme")

        result = source.mark_as_alias_of(target.id)

        self.assertEqual(result.canonical_id, target.id)
        target.refresh_from_db()
        self.assertIsNone(target.canonical_id)

    def test_self_target_raises(self):
        company = Company.objects.create(name="Acme Solo")
        with self.assertRaises(ValueError):
            company.mark_as_alias_of(company.id)

    def test_idempotent(self):
        target = Company.objects.create(name="Acme Real")
        source = Company.objects.create(name="Acme Alias")
        source.mark_as_alias_of(target.id)
        # Second call: no-op, no errors, same result.
        source.mark_as_alias_of(target.id)
        source.refresh_from_db()
        self.assertEqual(source.canonical_id, target.id)

    def test_chain_flattening_two_hops(self):
        """A→B, then B→C. After B aliases to C, A still points at B.
        Then A.mark_as_alias_of(B) — B is already aliased to C, so
        A should end up pointing at C, NOT B (one-hop invariant).
        """
        c = Company.objects.create(name="Acme Canonical")
        b = Company.objects.create(name="Acme Mid")
        a = Company.objects.create(name="Acme Tail")

        # A → B (b is canonical, no chain yet).
        a.mark_as_alias_of(b.id)
        a.refresh_from_db()
        self.assertEqual(a.canonical_id, b.id)

        # B → C. The invariant says A must now point at C, not B.
        b.mark_as_alias_of(c.id)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(b.canonical_id, c.id)
        self.assertEqual(
            a.canonical_id, c.id,
            "A was previously aliased AT B; when B got aliased to C, "
            "A must be re-pointed at C to preserve the one-hop invariant."
        )

    def test_aliasing_to_an_alias_jumps_to_root(self):
        """When target is itself an alias, mark_as_alias_of walks
        to the canonical root rather than creating a two-hop chain."""
        c = Company.objects.create(name="Acme Root")
        b = Company.objects.create(name="Acme Mid 2")
        a = Company.objects.create(name="Acme Leaf")
        b.mark_as_alias_of(c.id)
        # Now alias a to b. b's canonical is c, so a should end up at c.
        a.mark_as_alias_of(b.id)
        a.refresh_from_db()
        self.assertEqual(a.canonical_id, c.id)

    def test_cycle_raises(self):
        """A→B, then B→A: the second call must raise."""
        a = Company.objects.create(name="Acme Alpha")
        b = Company.objects.create(name="Acme Beta")

        a.mark_as_alias_of(b.id)

        # b is now canonical. Try to alias b to a — a's canonical
        # already points at b, so b.canonical = a creates a cycle.
        with self.assertRaises(ValueError):
            b.mark_as_alias_of(a.id)

        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.canonical_id, b.id)
        self.assertIsNone(b.canonical_id)


class TestMarkAsAliasOfEndpoint(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="u1", password="pw")
        self.staff = User.objects.create_user(
            username="staff1", password="pw", is_staff=True
        )
        self.source = Company.objects.create(name="Endpoint Source")
        self.target = Company.objects.create(name="Endpoint Target")

    def test_forbidden_for_non_staff(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(
            f"/api/v1/companies/{self.source.id}/mark-as-alias-of/",
            data={"target_id": self.target.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)
        self.source.refresh_from_db()
        self.assertIsNone(self.source.canonical_id)

    def test_ok_for_staff(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.post(
            f"/api/v1/companies/{self.source.id}/mark-as-alias-of/",
            data={"target_id": self.target.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.source.refresh_from_db()
        self.assertEqual(self.source.canonical_id, self.target.id)

    def test_missing_target_id_is_400(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.post(
            f"/api/v1/companies/{self.source.id}/mark-as-alias-of/",
            data={},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_invalid_target_id_is_404(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.post(
            f"/api/v1/companies/{self.source.id}/mark-as-alias-of/",
            data={"target_id": 999999},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_self_target_is_400(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.post(
            f"/api/v1/companies/{self.source.id}/mark-as-alias-of/",
            data={"target_id": self.source.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_jsonapi_body_shape_accepted(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.post(
            f"/api/v1/companies/{self.source.id}/mark-as-alias-of/",
            data={"data": {"attributes": {"target_id": self.target.id}}},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)


class TestUnmarkAsAliasOfEndpoint(TestCase):
    """Inverse verb — promote an alias Company back to canonical.

    Closes the Phase A loop: ``mark-as-alias-of`` sets the FK,
    ``unmark-as-alias-of`` clears it. Behavior parity with the
    mark verb: staff-only, 400 on no-op (already canonical), 404
    on unknown id.
    """

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="u_unmark", password="pw")
        self.staff = User.objects.create_user(
            username="staff_unmark", password="pw", is_staff=True
        )
        self.canonical = Company.objects.create(name="Unmark Canonical")
        self.alias = Company.objects.create(name="Unmark Alias")
        self.alias.canonical_id = self.canonical.id
        self.alias.save(update_fields=["canonical"])

    def test_forbidden_for_non_staff(self):
        self.client.force_authenticate(user=self.user)
        resp = self.client.post(
            f"/api/v1/companies/{self.alias.id}/unmark-as-alias-of/",
            data={},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)
        self.alias.refresh_from_db()
        # canonical_id unchanged.
        self.assertEqual(self.alias.canonical_id, self.canonical.id)

    def test_ok_for_staff_clears_canonical(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.post(
            f"/api/v1/companies/{self.alias.id}/unmark-as-alias-of/",
            data={},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.alias.refresh_from_db()
        self.assertIsNone(self.alias.canonical_id)

        # Response carries the updated Company resource.
        body = resp.json()
        self.assertEqual(body["data"]["id"], str(self.alias.id))
        self.assertEqual(body["data"]["type"], "company")

    def test_already_canonical_is_400(self):
        """A canonical Company (canonical_id IS NULL) is not a valid
        unmark target — explicit 400 so staff don't silently click
        the wrong row in the AliasesPanel."""
        self.client.force_authenticate(user=self.staff)
        resp = self.client.post(
            f"/api/v1/companies/{self.canonical.id}/unmark-as-alias-of/",
            data={},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.canonical.refresh_from_db()
        self.assertIsNone(self.canonical.canonical_id)

    def test_unknown_id_is_404(self):
        self.client.force_authenticate(user=self.staff)
        resp = self.client.post(
            "/api/v1/companies/9999999/unmark-as-alias-of/",
            data={},
            format="json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_no_payload_required(self):
        """Verb takes no body — both empty dict and empty string post
        should be accepted as long as the row is currently an alias."""
        self.client.force_authenticate(user=self.staff)
        resp = self.client.post(
            f"/api/v1/companies/{self.alias.id}/unmark-as-alias-of/",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.alias.refresh_from_db()
        self.assertIsNone(self.alias.canonical_id)


class TestAliasesSubCollection(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="u2", password="pw")
        self.client.force_authenticate(user=self.user)
        self.canonical = Company.objects.create(name="Canonical Sub")
        self.alias_a = Company.objects.create(name="Alias A Sub")
        self.alias_b = Company.objects.create(name="Alias B Sub")
        self.unrelated = Company.objects.create(name="Unrelated Sub")
        self.alias_a.canonical_id = self.canonical.id
        self.alias_a.save(update_fields=["canonical"])
        self.alias_b.canonical_id = self.canonical.id
        self.alias_b.save(update_fields=["canonical"])

    def test_lists_aliases_only(self):
        resp = self.client.get(
            f"/api/v1/companies/{self.canonical.id}/aliases/"
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        ids = {item["id"] for item in resp.json()["data"]}
        self.assertEqual(ids, {str(self.alias_a.id), str(self.alias_b.id)})

    def test_unknown_id_is_404(self):
        resp = self.client.get("/api/v1/companies/9999999/aliases/")
        self.assertEqual(resp.status_code, 404)


class TestCompanySerializerAliasRelationships(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username="u3", password="pw")
        self.client.force_authenticate(user=self.user)
        self.canonical = Company.objects.create(name="Ser Canonical")
        self.alias = Company.objects.create(name="Ser Alias")
        self.alias.canonical_id = self.canonical.id
        self.alias.save(update_fields=["canonical"])

    def test_links_only_by_default(self):
        """Without ?include=, relationships.aliases / canonical hold
        only `links` — no `data`. The JSON:API spec rule the project
        just shipped (Architecture/Audit notes)."""
        resp = self.client.get(f"/api/v1/companies/{self.canonical.id}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        rels = resp.json()["data"]["relationships"]
        self.assertIn("aliases", rels)
        self.assertIn("links", rels["aliases"])
        self.assertNotIn("data", rels["aliases"])
        self.assertNotIn("included", resp.json())

    def test_canonical_data_block_emitted_on_alias_row(self):
        """`canonical` is to-one. By the FK-fallback rule in
        BaseSerializer.to_resource, the `data` linkage is populated
        whenever canonical_id is non-NULL — uselist=False rels
        emit `data` unconditionally (FK identifier is cheap)."""
        resp = self.client.get(f"/api/v1/companies/{self.alias.id}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        rels = resp.json()["data"]["relationships"]
        self.assertIn("canonical", rels)
        self.assertEqual(
            rels["canonical"]["data"],
            {"type": "company", "id": str(self.canonical.id)},
        )

    def test_canonical_data_null_when_root(self):
        resp = self.client.get(f"/api/v1/companies/{self.canonical.id}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        rels = resp.json()["data"]["relationships"]
        self.assertIn("canonical", rels)
        self.assertIsNone(rels["canonical"]["data"])

    def test_include_aliases_populates_data_and_included(self):
        resp = self.client.get(
            f"/api/v1/companies/{self.canonical.id}/?include=aliases"
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        rels = body["data"]["relationships"]
        self.assertIn("data", rels["aliases"])
        self.assertEqual(
            rels["aliases"]["data"],
            [{"type": "company", "id": str(self.alias.id)}],
        )
        # Top-level included[] holds the alias Company resource.
        included = body.get("included", [])
        included_ids = {(r["type"], r["id"]) for r in included}
        self.assertIn(("company", str(self.alias.id)), included_ids)
