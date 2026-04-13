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
    healthcheck,
    guest_session,
    initialize,
    waitlist_signup,
    password_reset_request,
    password_reset_confirm,
    profile,
    career_data,
    career_data_export,
    career_data_import,
    generate_prompt,
)

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

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/healthcheck/", healthcheck, name="healthcheck"),
    re_path(r"^api/v1/healthcheck$", healthcheck, name="healthcheck-noslash"),
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
    re_path(
        r"^api/v1/companies/(?P<pk>\d+)/job-posts$",
        CompanyViewSet.as_view({"get": "job_posts"}),
        name="company-job-posts-noslash",
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
    path("api/v1/chat/", chat_proxy, name="chat"),
    path("api/v1/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/v1/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("api/v1/token/verify/", TokenVerifyView.as_view(), name="token_verify"),
    # path("api/v1/ping/", ping, name="ping"),
    # OpenAPI schema + UI
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/schema/swagger/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/schema/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]
