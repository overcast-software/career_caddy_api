"""Bust the cached public-demo report payload.

Call after `seed_demo_pipeline --reset` when you want the change to
reflect on `/reports/application-flow` immediately instead of waiting
for the PUBLIC_DEMO_CACHE_SECONDS TTL.
"""
from django.core.cache import cache
from django.core.management.base import BaseCommand

from job_hunting.api.views.reports import (
    PUBLIC_DEMO_FLOW_KEY,
    PUBLIC_DEMO_SOURCES_KEY,
)


class Command(BaseCommand):
    help = "Flush the cached public-demo application-flow + sources payloads"

    def handle(self, *args, **options):
        cache.delete(PUBLIC_DEMO_FLOW_KEY)
        cache.delete(PUBLIC_DEMO_SOURCES_KEY)
        self.stdout.write(self.style.SUCCESS("Public demo report cache flushed."))
