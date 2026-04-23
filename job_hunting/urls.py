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
    reconcile_onboarding,
    activity_report,
    application_flow_report,
    sources_report,
    report_filter_options,
    graph_structure,
    graph_aggregate,
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
        r"^api/v1/companies/(?P<pk>\d+)/job-posts$",
        CompanyViewSet.as_view({"get": "job_posts"}),
        name="company-job-posts-noslash",
    ),
    re_path(
        r"^api/v1/scrapes/(?P<pk>\d+)/screenshots/(?P<filename>.+\.png)$",
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
    path("api/v1/resumes/<int:pk>/markdown/", resume_markdown, name="resume-markdown"),
    path("api/v1/cover-letters/<int:pk>/markdown/", cover_letter_markdown, name="cover-letter-markdown"),
    path("api/v1/onboarding/reconcile/", reconcile_onboarding, name="onboarding-reconcile"),
    path("api/v1/reports/application-flow/", application_flow_report, name="reports-application-flow"),
    path("api/v1/reports/sources/", sources_report, name="reports-sources"),
    path("api/v1/reports/activity/", activity_report, name="reports-activity"),
    path("api/v1/reports/filter-options/", report_filter_options, name="reports-filter-options"),
    path("api/v1/admin/graph-structure/", graph_structure, name="graph-structure"),
    path("api/v1/admin/graph-aggregate/", graph_aggregate, name="graph-aggregate"),
    path("api/v1/chat/", chat_proxy, name="chat"),
    path("api/v1/token/", UnthrottledTokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/v1/token/refresh/", UnthrottledTokenRefreshView.as_view(), name="token_refresh"),
    path("api/v1/token/verify/", UnthrottledTokenVerifyView.as_view(), name="token_verify"),
    # path("api/v1/ping/", ping, name="ping"),
    # OpenAPI schema + UI
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/schema/swagger/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/schema/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]
