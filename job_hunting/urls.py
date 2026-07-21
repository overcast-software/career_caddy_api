"""job_hunting URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/3.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

from django.contrib import admin
from django.urls import path, include, re_path
from rest_framework import routers
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
    TokenVerifyView,
)


from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView
from job_hunting.api.chat import chat_proxy
from job_hunting.api.events import events_stream, events_token
from job_hunting.api.views.tasks_handlers import (
    cover_letter_task_handler,
    run_job_task_handler,
)
from job_hunting.api.views.federation import (
    actor_followers,
    actor_following,
    actor_inbox,
    actor_outbox,
    actor_view,
    company_actor_inbox,
    company_actor_view,
    company_followers,
    company_following,
    company_outbox,
    federation_activity_view,
    jobpost_object_view,
    webfinger,
)
from job_hunting.api.views import (
    DjangoUserViewSet,
    ResumeViewSet,
    ScoreViewSet,
    JobPostViewSet,
    ScrapeViewSet,
    CompanyViewSet,
    CoverLetterViewSet,
    JobApplicationViewSet,
    SummaryViewSet,
    ExperienceViewSet,
    EducationViewSet,
    CertificationViewSet,
    DescriptionViewSet,
    StatusViewSet,
    JobApplicationStatusViewSet,
    QuestionViewSet,
    AnswerViewSet,
    ApiKeyViewSet,
    ProjectViewSet,
    AiUsageViewSet,
    WaitlistViewSet,
    healthcheck,
    guest_session,
    agent_models,
    initialize,
    waitlist_signup,
    password_reset_request,
    password_reset_confirm,
    InvitationViewSet,
    ScrapeProfileViewSet,
    accept_invite,
    test_email,
    profile,
    career_data,
    career_data_export,
    career_data_import,
    generate_prompt,
    resume_markdown,
    cover_letter_markdown,
    onboarding_endpoint,
    reconcile_onboarding,
    activity_report,
    application_flow_report,
    dedupe_feedback_report,
    sources_report,
    report_filter_options,
    graph_structure,
    graph_aggregate,
    graph_mermaid,
    scrape_queue_health,
    public_user_federated_job_posts,
    public_user_profile,
    public_user_application_flow,
)


class UnthrottledTokenObtainPairView(TokenObtainPairView):
    throttle_classes = []


class UnthrottledTokenRefreshView(TokenRefreshView):
    throttle_classes = []


class UnthrottledTokenVerifyView(TokenVerifyView):
    throttle_classes = []


router = routers.DefaultRouter(trailing_slash="/?")
router.register(r"users", DjangoUserViewSet, basename="users")
router.register(r"resumes", ResumeViewSet, basename="resumes")
router.register(r"scores", ScoreViewSet, basename="scores")
router.register(r"job-posts", JobPostViewSet, basename="job-posts")
router.register(r"scrapes", ScrapeViewSet, basename="scrapes")
router.register(r"companies", CompanyViewSet, basename="companies")
router.register(r"cover-letters", CoverLetterViewSet, basename="cover-letters")
router.register(r"job-applications", JobApplicationViewSet, basename="job-applications")
router.register(r"summaries", SummaryViewSet, basename="summaries")
router.register(r"experiences", ExperienceViewSet, basename="experiences")
router.register(r"educations", EducationViewSet, basename="educations")
router.register(r"certifications", CertificationViewSet, basename="certifications")
router.register(r"descriptions", DescriptionViewSet, basename="descriptions")
router.register(r"statuses", StatusViewSet, basename="statuses")
router.register(r"job-application-statuses", JobApplicationStatusViewSet, basename="job-application-statuses")
router.register(r"questions", QuestionViewSet, basename="questions")
router.register(r"answers", AnswerViewSet, basename="answers")
router.register(r"api-keys", ApiKeyViewSet, basename="api-keys")
router.register(r"projects", ProjectViewSet, basename="projects")
router.register(r"ai-usages", AiUsageViewSet, basename="ai-usages")
router.register(r"waitlists", WaitlistViewSet, basename="waitlists")
router.register(r"invitations", InvitationViewSet, basename="invitations")
router.register(r"scrape-profiles", ScrapeProfileViewSet, basename="scrape-profiles")

urlpatterns = [
    path("admin/", admin.site.urls),
    # ActivityPub Phase 5a — root-URL routes (NOT under /api/v1/).
    # WebFinger is RFC 7033 mandated at .well-known; Actor URIs mirror
    # what job_hunting.lib.as_object.actor_uri has emitted since
    # Phase 4. Both views are public — federation peers (Mastodon, etc.)
    # have no auth context on first contact.
    path(".well-known/webfinger", webfinger, name="webfinger"),
    re_path(r"^\.well-known/webfinger/?$", webfinger, name="webfinger-trailing"),
    path("actors/<slug:username>/", actor_view, name="actor"),
    re_path(r"^actors/(?P<username>[-\w]+)$", actor_view, name="actor-noslash"),
    # Phase 5a collection stubs — empty OrderedCollection bodies so AP
    # peers enumerating the Actor JSON don't trip Django's HTML 404
    # template and flag the actor as broken. Real outbox / followers /
    # following enumeration is Phase 5b/5c.
    path("actors/<str:username>/outbox", actor_outbox, name="actor-outbox"),
    path("actors/<str:username>/outbox/", actor_outbox, name="actor-outbox-slash"),
    # Phase 5c — authenticated AP inbox. HTTP-Signature verified at the
    # view boundary, no Django auth / DRF permissions on this path.
    path("actors/<str:username>/inbox", actor_inbox, name="actor-inbox"),
    path("actors/<str:username>/inbox/", actor_inbox, name="actor-inbox-slash"),
    path("actors/<str:username>/followers", actor_followers, name="actor-followers"),
    path("actors/<str:username>/followers/", actor_followers, name="actor-followers-slash"),
    path("actors/<str:username>/following", actor_following, name="actor-following"),
    path("actors/<str:username>/following/", actor_following, name="actor-following-slash"),
    # Phase 6a — Company/Organization actors. Content-negotiated:
    # AS2 JSON for federation peers, JSON:API stub for browsers/SPA.
    # Slug-routed at the root so Mastodon's WebFinger handoff lands
    # directly on the public Company page URL.
    path("companies/<slug:slug>/", company_actor_view, name="company-actor"),
    re_path(
        r"^companies/(?P<slug>[-\w]+)$", company_actor_view, name="company-actor-noslash"
    ),
    path("companies/<slug:slug>/outbox", company_outbox, name="company-outbox"),
    path(
        "companies/<slug:slug>/outbox/", company_outbox, name="company-outbox-slash"
    ),
    # Phase 6b — Company-actor inbox + Follow handshake.
    path(
        "companies/<slug:slug>/inbox",
        company_actor_inbox,
        name="company-actor-inbox",
    ),
    path(
        "companies/<slug:slug>/inbox/",
        company_actor_inbox,
        name="company-actor-inbox-slash",
    ),
    path(
        "companies/<slug:slug>/followers",
        company_followers,
        name="company-followers",
    ),
    path(
        "companies/<slug:slug>/followers/",
        company_followers,
        name="company-followers-slash",
    ),
    path(
        "companies/<slug:slug>/following",
        company_following,
        name="company-following",
    ),
    path(
        "companies/<slug:slug>/following/",
        company_following,
        name="company-following-slash",
    ),
    # BACK-93 — AP object dereferencing. Root-URL (NOT /api/v1/) so they
    # match the object/activity ids the outbox advertises:
    # ``object.id = {origin}/job-posts/<pk>`` and
    # ``id = {origin}/activities/<uuid>``. ``/job-posts/<pk>`` is
    # content-negotiated (AS2 Note on AP Accept, JSON:API stub otherwise)
    # so the SPA's human-facing /job-posts/<id> route is undisturbed when
    # the apex routes only AP-Accept traffic here. ``/activities/<uuid>``
    # is federation-only (no SPA sibling), always AS2.
    path("job-posts/<str:pk>", jobpost_object_view, name="jobpost-object"),
    path("job-posts/<str:pk>/", jobpost_object_view, name="jobpost-object-slash"),
    re_path(
        r"^activities/(?P<activity_uuid>[0-9a-fA-F-]+)$",
        federation_activity_view,
        name="federation-activity",
    ),
    re_path(
        r"^activities/(?P<activity_uuid>[0-9a-fA-F-]+)/$",
        federation_activity_view,
        name="federation-activity-slash",
    ),
    path("api/v1/healthcheck/", healthcheck, name="healthcheck"),
    re_path(r"^api/v1/healthcheck$", healthcheck, name="healthcheck-noslash"),
    path("api/v1/agent-models/", agent_models, name="agent-models"),
    re_path(r"^api/v1/agent-models$", agent_models, name="agent-models-noslash"),
    path("api/v1/guest-session/", guest_session, name="guest-session"),
    re_path(r"^api/v1/guest-session$", guest_session, name="guest-session-noslash"),
    path("api/v1/initialize/", initialize, name="initialize"),
    re_path(r"^api/v1/initialize$", initialize, name="initialize-noslash"),
    path("api/v1/waitlist/", waitlist_signup, name="waitlist"),
    re_path(r"^api/v1/waitlist$", waitlist_signup, name="waitlist-noslash"),
    path("api/v1/password-reset/", password_reset_request, name="password-reset"),
    re_path(r"^api/v1/password-reset$", password_reset_request, name="password-reset-noslash"),
    path("api/v1/password-reset/confirm/", password_reset_confirm, name="password-reset-confirm"),
    re_path(r"^api/v1/password-reset/confirm$", password_reset_confirm, name="password-reset-confirm-noslash"),
    path("api/v1/accept-invite/", accept_invite, name="accept-invite"),
    re_path(r"^api/v1/accept-invite$", accept_invite, name="accept-invite-noslash"),
    path("api/v1/test-email/", test_email, name="test-email"),
    re_path(r"^api/v1/test-email$", test_email, name="test-email-noslash"),
    re_path(
        r"^api/v1/companies/(?P<pk>[0-9A-Za-z]+)/job-posts$",
        CompanyViewSet.as_view({"get": "job_posts"}),
        name="company-job-posts-noslash",
    ),
    # CC #51 — public (AllowAny) read of a user's FEDERATED (audience-public)
    # job posts; powers the public /<username> profile page. Username (not
    # id) is the path key. Trailing slash optional (router dual-slash
    # convention). Placed before the router include so the nested collection
    # route wins; only the /federated subset is ever publicly routed.
    re_path(
        r"^api/v1/users/(?P<username>[^/]+)/job-posts/federated/?$",
        public_user_federated_job_posts,
        name="user-federated-job-posts",
    ),
    # CC-105 — public (AllowAny) application-flow (Sankey) funnel for the
    # /<username> profile header. Same published-post selection + username
    # resolution as the federated feed above; gated on the owner's
    # federate_rich opt-in and degrades to an empty flow (200, never 403/404).
    # Multi-segment path so the numeric-guarded /users/<username>/ catch-all
    # below never shadows it; placed before the router include all the same.
    re_path(
        r"^api/v1/users/(?P<username>[^/]+)/application-flow/?$",
        public_user_application_flow,
        name="public-user-application-flow",
    ),
    # CC #51 — public (AllowAny) read of a single user as a JSON:API `user`
    # resource (the federated relationship owner; powers the /<username>
    # profile header). Username is the lookup key; the resource id stays
    # the canonical numeric id. The `(?!\d+/?$)` guard makes a PURELY
    # NUMERIC segment (e.g. /users/5/) fall THROUGH to the router's authed
    # numeric-pk retrieve route, so this never shadows GET /users/<id>/.
    re_path(
        r"^api/v1/users/(?P<username>(?!\d+/?$)[^/]+)/?$",
        public_user_profile,
        name="public-user-profile",
    ),
    re_path(
        r"^api/v1/scrapes/(?P<pk>[0-9A-Za-z]+)/screenshots/(?P<filename>.+\.png)$",
        ScrapeViewSet.as_view({"get": "screenshot_file"}),
        name="scrape-screenshot-file-direct",
    ),
    path("api/v1/", include(router.urls)),
    path(
        "api/v1/auth/register/",
        DjangoUserViewSet.as_view({"post": "create"}),
        name="auth-register",
    ),
    path(
        "api/v1/auth/bootstrap-superuser/",
        DjangoUserViewSet.as_view({"post": "bootstrap_superuser"}),
        name="auth-bootstrap-superuser",
    ),
    path("api/v1/me/", DjangoUserViewSet.as_view({"get": "me"}), name="me"),
    path("api/v1/profile/", profile, name="profile"),
    path("api/v1/career-data/", career_data, name="career-data"),
    path("api/v1/users/<int:user_id>/career-data/", career_data, name="user-career-data"),
    path("api/v1/career-data/export/", career_data_export, name="career-data-export"),
    path("api/v1/career-data/import/", career_data_import, name="career-data-import"),
    path("api/v1/generate-prompt/", generate_prompt, name="generate-prompt"),
    path("api/v1/resumes/<str:pk>/markdown/", resume_markdown, name="resume-markdown"),
    path("api/v1/cover-letters/<str:pk>/markdown/", cover_letter_markdown, name="cover-letter-markdown"),
    path(
        "api/v1/users/<str:user_id>/onboarding/",
        onboarding_endpoint,
        name="onboarding-endpoint",
    ),
    path(
        "api/v1/users/<str:user_id>/onboarding/reconcile/",
        reconcile_onboarding,
        name="onboarding-reconcile",
    ),
    path("api/v1/reports/application-flow/", application_flow_report, name="reports-application-flow"),
    path("api/v1/reports/sources/", sources_report, name="reports-sources"),
    path("api/v1/reports/activity/", activity_report, name="reports-activity"),
    path("api/v1/reports/dedupe-feedback/", dedupe_feedback_report, name="reports-dedupe-feedback"),
    path("api/v1/reports/filter-options/", report_filter_options, name="reports-filter-options"),
    path("api/v1/admin/graph-structure/", graph_structure, name="graph-structure"),
    path("api/v1/admin/graph-aggregate/", graph_aggregate, name="graph-aggregate"),
    path("api/v1/admin/graph-mermaid/", graph_mermaid, name="graph-mermaid"),
    path(
        "api/v1/admin/scrape-queue-health/",
        scrape_queue_health,
        name="scrape-queue-health",
    ),
    re_path(
        r"^api/v1/admin/scrape-queue-health$",
        scrape_queue_health,
        name="scrape-queue-health-noslash",
    ),
    # CC-169 — Cloud Tasks HTTP handler (plain JSON, NOT JSON:API). Served by
    # the IAM-private `tasks` Cloud Run service (same api image). The path
    # MUST match the terraform + producer (job_hunting.lib.cloud_tasks).
    path(
        "tasks/cover-letter/",
        cover_letter_task_handler,
        name="tasks-cover-letter",
    ),
    re_path(
        r"^tasks/cover-letter$",
        cover_letter_task_handler,
        name="tasks-cover-letter-noslash",
    ),
    # CC-214 — generic Cloud Tasks handler ({kind, payload}). Dispatches any
    # registered kind through job_hunting.lib.job_kinds. Same IAM-private
    # `tasks` Cloud Run service; path MUST match the producer + terraform.
    path(
        "tasks/run-job/",
        run_job_task_handler,
        name="tasks-run-job",
    ),
    re_path(
        r"^tasks/run-job$",
        run_job_task_handler,
        name="tasks-run-job-noslash",
    ),
    path("api/v1/chat/", chat_proxy, name="chat"),
    # SSE — Phase 2 of Plans/Push status updates. Issue token, then stream.
    path("api/v1/events/token/", events_token, name="events-token"),
    path("api/v1/events/", events_stream, name="events-stream"),
    path("api/v1/token/", UnthrottledTokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/v1/token/refresh/", UnthrottledTokenRefreshView.as_view(), name="token_refresh"),
    path("api/v1/token/verify/", UnthrottledTokenVerifyView.as_view(), name="token_verify"),
    # path("api/v1/ping/", ping, name="ping"),
    # OpenAPI schema + UI
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/schema/swagger/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/schema/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]
