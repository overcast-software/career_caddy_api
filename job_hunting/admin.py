from django.conf import settings
from django.contrib import admin

from job_hunting.models import (
    FederationActivity,
    FederationFollower,
    JobPost,
    Waitlist,
)


@admin.register(Waitlist)
class WaitlistAdmin(admin.ModelAdmin):
    list_display = ("email", "created_at")
    list_filter = ("created_at",)
    search_fields = ("email",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(FederationFollower)
class FederationFollowerAdmin(admin.ModelAdmin):
    """Browse remote followers — useful for sanity-checking a new Follow
    after a peer points at our instance and for diagnosing Accept(Follow)
    delivery failures (look for accepted_at IS NULL).
    """

    list_display = (
        "actor_uri",
        "instance_host",
        "local_user",
        "accepted_at",
        "unfollowed_at",
        "created_at",
    )
    list_filter = ("instance_host", "accepted_at", "unfollowed_at")
    search_fields = ("actor_uri", "inbox_uri", "instance_host")
    readonly_fields = ("created_at", "updated_at")


@admin.register(FederationActivity)
class FederationActivityAdmin(admin.ModelAdmin):
    """Audit log for inbound + outbound AP activities.

    Dead-letter rows are the operator's first surface during a
    federation incident — filter on delivery_status='dead_letter' to
    see what didn't make it out. The body column is full activity
    JSON; readonly here so a debug-poking superuser can't mutate
    delivered history.
    """

    list_display = (
        "direction",
        "activity_type",
        "delivery_status",
        "actor_uri",
        "target_uri",
        "retry_count",
        "next_attempt_at",
        "created_at",
    )
    list_filter = (
        "direction",
        "activity_type",
        "delivery_status",
        "created_at",
    )
    search_fields = ("activity_id", "actor_uri", "target_uri")
    readonly_fields = (
        "direction",
        "activity_type",
        "activity_id",
        "actor_uri",
        "target_uri",
        "local_user",
        "body",
        "signature_payload",
        "received_at",
        "delivered_at",
        "delivery_status",
        "delivery_error",
        "retry_count",
        "next_attempt_at",
        "created_at",
    )


class FederatedJobPostFilter(admin.SimpleListFilter):
    """Federated-origin filter for JobPost admin.

    Local rows carry ``source_instance == CAREER_CADDY_INSTANCE``;
    federated rows carry the remote peer host. The "federated" option
    excludes the local default; the "local" option is the inverse;
    "all" (no filter) falls through to Django's default queryset so the
    list page still works when none of the radio buttons is selected.
    """

    title = "origin"
    parameter_name = "origin"

    def lookups(self, request, model_admin):
        return [
            ("federated", "Federated (remote)"),
            ("local", "Local"),
        ]

    def queryset(self, request, queryset):
        local = settings.CAREER_CADDY_INSTANCE
        if self.value() == "federated":
            return queryset.exclude(source_instance=local)
        if self.value() == "local":
            return queryset.filter(source_instance=local)
        return queryset


@admin.register(JobPost)
class JobPostAdmin(admin.ModelAdmin):
    """Minimal admin for JobPost — primary value is the federated filter.

    The 5e ingest path is the first one that lands rows whose
    ``source_instance`` doesn't match this instance, so the admin
    surface lets an operator see at-a-glance which peers have
    contributed federated content + spot federated rows that need to be
    promoted into a local user's queue via JobPostDiscovery. Not a
    general JobPost browsing UI — that's the frontend's job — so most
    columns stay off.
    """

    list_display = (
        "id",
        "title",
        "source",
        "source_instance",
        "posting_status",
        "created_at",
    )
    list_filter = (
        FederatedJobPostFilter,
        "source",
        "posting_status",
        "complete",
    )
    search_fields = ("title", "link", "canonical_link", "source_instance")
    readonly_fields = (
        "created_at",
        "canonical_link",
        "content_fingerprint",
        "source_instance",
    )
