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
from django.urls import path, include
from rest_framework import routers
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
    TokenVerifyView,
)
from job_hunting.api.views import (
    DjangoUserViewSet,
    ResumeViewSet,
    ScoreViewSet,
    JobPostViewSet,
    ScrapeViewSet,
    CompanyViewSet,
    CoverLetterViewSet,
    ApplicationViewSet,
    SummaryViewSet,
    ExperienceViewSet,
    EducationViewSet,
    CertificationViewSet,
    DescriptionViewSet,
    healthcheck,
)

router = routers.DefaultRouter()
router.register(r"users", DjangoUserViewSet, basename="users")
router.register(r"resumes", ResumeViewSet, basename="resumes")
router.register(r"scores", ScoreViewSet, basename="scores")
router.register(r"job-posts", JobPostViewSet, basename="job-posts")
router.register(r"scrapes", ScrapeViewSet, basename="scrapes")
router.register(r"companies", CompanyViewSet, basename="companies")
router.register(r"cover-letters", CoverLetterViewSet, basename="cover-letters")
router.register(r"applications", ApplicationViewSet, basename="applications")
router.register(r"summaries", SummaryViewSet, basename="summaries")
router.register(r"experiences", ExperienceViewSet, basename="experiences")
router.register(r"educations", EducationViewSet, basename="educations")
router.register(r"certifications", CertificationViewSet, basename="certifications")
router.register(r"descriptions", DescriptionViewSet, basename="descriptions")

urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthcheck/", healthcheck, name="healthcheck"),
    path("api/v1/", include(router.urls)),
    path("api/v1/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/v1/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("api/v1/token/verify/", TokenVerifyView.as_view(), name="token_verify"),
    # path("api/v1/ping/", ping, name="ping"),
]
