from django.contrib import admin
from job_hunting.models import Waitlist


@admin.register(Waitlist)
class WaitlistAdmin(admin.ModelAdmin):
    list_display = ("email", "created_at")
    list_filter = ("created_at",)
    search_fields = ("email",)
    readonly_fields = ("created_at", "updated_at")
