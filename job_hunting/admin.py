from django.contrib import admin

from job_hunting.models import FederationActivity, FederationFollower, Waitlist


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
