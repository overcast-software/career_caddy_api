from .auth import (
    profile,
    healthcheck,
    guest_session,
    initialize,
    waitlist_signup,
    password_reset_request,
    password_reset_confirm,
    accept_invite,
    test_email,
)
from .users import DjangoUserViewSet
from .resumes import ResumeViewSet
from .summaries import SummaryViewSet
from .scores import ScoreViewSet
from .jobs import (
    JobPostViewSet,
    JobApplicationViewSet,
    StatusViewSet,
    JobApplicationStatusViewSet,
)
from .scrapes import ScrapeViewSet, ScrapeProfileViewSet
from .companies import CompanyViewSet
from .cover_letters import CoverLetterViewSet
from .resume_parts import (
    ExperienceViewSet,
    EducationViewSet,
    CertificationViewSet,
    DescriptionViewSet,
    ProjectViewSet,
)
from .questions import QuestionViewSet, AnswerViewSet
from .admin import (
    ApiKeyViewSet,
    AiUsageViewSet,
    WaitlistViewSet,
    InvitationViewSet,
    agent_models,
)
from .career_data import (
    career_data,
    generate_prompt,
    career_data_export,
    career_data_import,
)
from .markdown import (
    cover_letter_markdown,
    resume_markdown,
)
from .onboarding import reconcile_onboarding
from .reports import (
    activity_report,
    application_flow_report,
    sources_report,
    report_filter_options,
)
from .graph import (
    graph_structure,
    graph_aggregate,
    graph_mermaid,
)

__all__ = [
    # ViewSets
    "DjangoUserViewSet",
    "ResumeViewSet",
    "ScoreViewSet",
    "JobPostViewSet",
    "ScrapeViewSet",
    "CompanyViewSet",
    "CoverLetterViewSet",
    "JobApplicationViewSet",
    "SummaryViewSet",
    "ExperienceViewSet",
    "EducationViewSet",
    "CertificationViewSet",
    "DescriptionViewSet",
    "StatusViewSet",
    "JobApplicationStatusViewSet",
    "QuestionViewSet",
    "AnswerViewSet",
    "ApiKeyViewSet",
    "ProjectViewSet",
    "AiUsageViewSet",
    "WaitlistViewSet",
    "InvitationViewSet",
    "ScrapeProfileViewSet",
    # Function views
    "healthcheck",
    "guest_session",
    "agent_models",
    "initialize",
    "waitlist_signup",
    "password_reset_request",
    "password_reset_confirm",
    "accept_invite",
    "test_email",
    "profile",
    "career_data",
    "career_data_export",
    "career_data_import",
    "generate_prompt",
    "resume_markdown",
    "cover_letter_markdown",
    "reconcile_onboarding",
    "activity_report",
    "application_flow_report",
    "sources_report",
    "report_filter_options",
    "graph_structure",
    "graph_aggregate",
    "graph_mermaid",
]
