"""Inventory + idempotency tests for migration 0062 seed.

The seed migration runs at TestCase setup (Django re-applies migrations
against the test DB), so we just inspect the post-migration ScrapeProfile
table.
"""
import importlib

from django.apps import apps as global_apps
from django.test import TestCase

from job_hunting.models import ScrapeProfile


_seed_mod = importlib.import_module(
    "job_hunting.migrations.0062_seed_apply_resolver_configs"
)
SEEDS = _seed_mod.SEEDS


class SeedApplyResolverConfigTests(TestCase):

    def test_every_host_has_a_profile_with_config(self):
        for hostname, expected in SEEDS.items():
            profile = ScrapeProfile.objects.get(hostname=hostname)
            self.assertEqual(
                profile.apply_resolver_config,
                expected,
                msg=f"seed mismatch for {hostname}",
            )

    def test_config_shape_per_host(self):
        for hostname, config in SEEDS.items():
            self.assertEqual(
                set(config.keys()),
                {
                    "internal_apply_markers",
                    "apply_link_selectors",
                    "apply_button_selectors",
                },
                msg=f"unexpected keys for {hostname}",
            )
            for key, value in config.items():
                self.assertIsInstance(
                    value, list, msg=f"{hostname}.{key} must be a list"
                )

    def test_seed_is_idempotent_and_respects_operator_overrides(self):
        profile = ScrapeProfile.objects.get(hostname="linkedin.com")
        operator_config = {"internal_apply_markers": ["#operator"]}
        profile.apply_resolver_config = operator_config
        profile.save(update_fields=["apply_resolver_config"])

        _seed_mod.seed(global_apps, None)

        profile.refresh_from_db()
        self.assertEqual(profile.apply_resolver_config, operator_config)

    def test_minimum_host_inventory(self):
        expected = {
            "linkedin.com",
            "greenhouse.io",
            "boards.greenhouse.io",
            "lever.co",
            "jobs.lever.co",
            "jobot.com",
            "indeed.com",
            "ziprecruiter.com",
        }
        self.assertEqual(set(SEEDS.keys()), expected)
