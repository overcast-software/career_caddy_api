"""
Pydantic models generated from OpenAPI schema.
These models can be used for validation, serialization, and type hints.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Union
from enum import Enum

from pydantic import BaseModel, Field, EmailStr, HttpUrl


# Enums
class FormatEnum(str, Enum):
    json = "json"
    vnd_api_json = "vnd.api+json"


class StatusTypeEnum(str, Enum):
    # Add specific status types as needed
    pass


# Base JSON:API structures
class JsonApiResourceIdentifier(BaseModel):
    """JSON:API resource identifier object."""
    type: str
    id: str


class JsonApiRelationship(BaseModel):
    """JSON:API relationship object."""
    data: Optional[Union[JsonApiResourceIdentifier, List[JsonApiResourceIdentifier]]] = None
    links: Optional[Dict[str, Any]] = None
    meta: Optional[Dict[str, Any]] = None


class JsonApiResource(BaseModel):
    """JSON:API resource object."""
    type: str
    id: Optional[str] = None
    attributes: Optional[Dict[str, Any]] = None
    relationships: Optional[Dict[str, JsonApiRelationship]] = None
    links: Optional[Dict[str, Any]] = None
    meta: Optional[Dict[str, Any]] = None


class JsonApiItem(BaseModel):
    """JSON:API single resource response."""
    data: JsonApiResource
    included: Optional[List[JsonApiResource]] = None
    links: Optional[Dict[str, Any]] = None
    meta: Optional[Dict[str, Any]] = None


class JsonApiList(BaseModel):
    """JSON:API collection response."""
    data: List[JsonApiResource]
    included: Optional[List[JsonApiResource]] = None
    links: Optional[Dict[str, Any]] = None
    meta: Optional[Dict[str, Any]] = None


class JsonApiWrite(BaseModel):
    """JSON:API write request."""
    data: JsonApiResource = Field(
        ...,
        description="JSON:API resource object with 'type', 'attributes', and optional 'relationships'."
    )


class PatchedJsonApiWrite(BaseModel):
    """JSON:API partial write request."""
    data: Optional[JsonApiResource] = Field(
        None,
        description="JSON:API resource object with 'type', 'attributes', and optional 'relationships'."
    )


# Request/Response models
class AnswerCreateRequest(BaseModel):
    """Request to create an answer."""
    question_id: int = Field(..., description="Required. ID of the parent Question.")
    content: Optional[str] = Field(None, description="Answer text. Required unless ai_assist=true.")
    ai_assist: Optional[bool] = Field(None, description="If true and content is empty, AI generates the answer.")
    injected_prompt: Optional[str] = Field(None, description="Optional custom prompt injected into AI generation.")


class ApiKeyCreateRequest(BaseModel):
    """Request to create an API key."""
    name: str = Field(..., description="Human-readable name for this key")
    expires_days: Optional[int] = Field(None, description="Days until expiry (omit for no expiry)")
    scopes: Optional[List[str]] = Field(None, description="Defaults to ['read', 'write']")


class CareerDataResponse(BaseModel):
    """Career data response."""
    data: str


class GeneratePromptData(BaseModel):
    """Generated prompt data."""
    prompt: str
    context: Dict[str, Any]


class GeneratePromptRequest(BaseModel):
    """Request to generate a prompt."""
    question_id: int = Field(..., description="Required. ID of the Question to answer.")
    job_post_id: Optional[int] = Field(None, description="Optional job post context.")
    resume_id: Optional[int] = Field(None, description="Optional resume to include.")
    instructions: Optional[str] = Field(None, description="Custom instructions appended to the prompt.")


class GeneratePromptResponse(BaseModel):
    """Response with generated prompt."""
    data: GeneratePromptData


class IngestResumeRequest(BaseModel):
    """Request to ingest a resume from file."""
    file: HttpUrl = Field(..., description="DOCX resume file (multipart/form-data)")


class ProfileResponse(BaseModel):
    """User profile response."""
    data: Dict[str, Any]


class ScrapeCreateRequest(BaseModel):
    """Request to create a scrape."""
    url: HttpUrl = Field(..., description="URL to scrape")


class SetOpenAIKeyRequest(BaseModel):
    """Request to set OpenAI API key."""
    openai_api_key: str


class SetOpenAIKeyResponse(BaseModel):
    """Response after setting OpenAI API key."""
    meta: Dict[str, Any]


class Status(BaseModel):
    """Status model."""
    id: Optional[int] = Field(None, readOnly=True)
    status: str = Field(..., max_length=255)
    status_type: Optional[str] = Field(None, max_length=255)
    created_at: Optional[datetime] = Field(None, readOnly=True)


class PatchedStatus(BaseModel):
    """Partial status update."""
    id: Optional[int] = Field(None, readOnly=True)
    status: Optional[str] = Field(None, max_length=255)
    status_type: Optional[str] = Field(None, max_length=255)
    created_at: Optional[datetime] = Field(None, readOnly=True)


class TokenObtainPair(BaseModel):
    """JWT token pair."""
    username: Optional[str] = Field(None, writeOnly=True)
    password: Optional[str] = Field(None, writeOnly=True)
    access: Optional[str] = Field(None, readOnly=True)
    refresh: Optional[str] = Field(None, readOnly=True)


class TokenRefresh(BaseModel):
    """JWT token refresh."""
    access: Optional[str] = Field(None, readOnly=True)
    refresh: str


class TokenVerify(BaseModel):
    """JWT token verification."""
    token: str = Field(..., writeOnly=True)


class UserWriteRequest(BaseModel):
    """Request to create/update a user."""
    username: Optional[str] = None
    email: Optional[EmailStr] = None
    password: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None


class PatchedUserWriteRequest(BaseModel):
    """Partial user update request."""
    username: Optional[str] = None
    email: Optional[EmailStr] = None
    password: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None


class UserResource(BaseModel):
    """User resource response."""
    data: Dict[str, Any]


class UserListResource(BaseModel):
    """User list response."""
    data: List[Dict[str, Any]]
    included: Optional[List[Dict[str, Any]]] = None


# Domain-specific attribute models (for type safety)
class AnswerAttributes(BaseModel):
    """Answer resource attributes."""
    content: Optional[str] = None
    ai_assist: Optional[bool] = None
    injected_prompt: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ApiKeyAttributes(BaseModel):
    """API Key resource attributes."""
    name: str
    key: Optional[str] = Field(None, description="Plain key only shown on creation")
    expires_at: Optional[datetime] = None
    scopes: Optional[List[str]] = None
    created_at: Optional[datetime] = None
    is_active: Optional[bool] = None


class CertificationAttributes(BaseModel):
    """Certification resource attributes."""
    name: Optional[str] = None
    issuer: Optional[str] = None
    date_obtained: Optional[str] = None
    expiration_date: Optional[str] = None
    credential_id: Optional[str] = None
    credential_url: Optional[str] = None


class CompanyAttributes(BaseModel):
    """Company resource attributes."""
    name: Optional[str] = None
    website: Optional[str] = None
    description: Optional[str] = None
    industry: Optional[str] = None
    size: Optional[str] = None
    location: Optional[str] = None


class CoverLetterAttributes(BaseModel):
    """Cover letter resource attributes."""
    content: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class DescriptionAttributes(BaseModel):
    """Description resource attributes."""
    text: Optional[str] = None
    order: Optional[int] = None


class EducationAttributes(BaseModel):
    """Education resource attributes."""
    institution: Optional[str] = None
    degree: Optional[str] = None
    field_of_study: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    gpa: Optional[str] = None
    description: Optional[str] = None


class ExperienceAttributes(BaseModel):
    """Experience resource attributes."""
    company: Optional[str] = None
    title: Optional[str] = None
    location: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    current: Optional[bool] = None
    description: Optional[str] = None


class JobApplicationAttributes(BaseModel):
    """Job application resource attributes."""
    applied_at: Optional[datetime] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class JobApplicationStatusAttributes(BaseModel):
    """Job application status resource attributes."""
    status: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[datetime] = None


class JobPostAttributes(BaseModel):
    """Job post resource attributes."""
    title: Optional[str] = None
    description: Optional[str] = None
    location: Optional[str] = None
    remote: Optional[bool] = None
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    posted_date: Optional[str] = None
    extraction_date: Optional[str] = None
    link: Optional[str] = None
    created_at: Optional[datetime] = None


class QuestionAttributes(BaseModel):
    """Question resource attributes."""
    text: Optional[str] = None
    question_type: Optional[str] = None
    required: Optional[bool] = None
    max_length: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ResumeAttributes(BaseModel):
    """Resume resource attributes."""
    name: Optional[str] = None
    summary: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    location: Optional[str] = None
    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    website_url: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ScoreAttributes(BaseModel):
    """Score resource attributes."""
    score: Optional[float] = None
    reasoning: Optional[str] = None
    strengths: Optional[List[str]] = None
    weaknesses: Optional[List[str]] = None
    recommendations: Optional[List[str]] = None
    created_at: Optional[datetime] = None


class ScrapeAttributes(BaseModel):
    """Scrape resource attributes."""
    url: str
    status: Optional[str] = None  # pending, processing, completed, failed
    content: Optional[str] = None
    error_message: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class SkillAttributes(BaseModel):
    """Skill resource attributes."""
    text: Optional[str] = None
    category: Optional[str] = None
    proficiency: Optional[str] = None


class SummaryAttributes(BaseModel):
    """Summary resource attributes."""
    content: Optional[str] = None
    is_active: Optional[bool] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class UserAttributes(BaseModel):
    """User resource attributes."""
    username: str
    email: Optional[EmailStr] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    is_staff: Optional[bool] = None
    is_active: Optional[bool] = None
    date_joined: Optional[datetime] = None


# Typed resource models (combining type + attributes)
class AnswerResource(JsonApiResource):
    """Typed Answer resource."""
    type: str = Field(default="answer", const=True)
    attributes: Optional[AnswerAttributes] = None


class ApiKeyResource(JsonApiResource):
    """Typed API Key resource."""
    type: str = Field(default="api-key", const=True)
    attributes: Optional[ApiKeyAttributes] = None


class CertificationResource(JsonApiResource):
    """Typed Certification resource."""
    type: str = Field(default="certification", const=True)
    attributes: Optional[CertificationAttributes] = None


class CompanyResource(JsonApiResource):
    """Typed Company resource."""
    type: str = Field(default="company", const=True)
    attributes: Optional[CompanyAttributes] = None


class CoverLetterResource(JsonApiResource):
    """Typed Cover Letter resource."""
    type: str = Field(default="cover-letter", const=True)
    attributes: Optional[CoverLetterAttributes] = None


class DescriptionResource(JsonApiResource):
    """Typed Description resource."""
    type: str = Field(default="description", const=True)
    attributes: Optional[DescriptionAttributes] = None


class EducationResource(JsonApiResource):
    """Typed Education resource."""
    type: str = Field(default="education", const=True)
    attributes: Optional[EducationAttributes] = None


class ExperienceResource(JsonApiResource):
    """Typed Experience resource."""
    type: str = Field(default="experience", const=True)
    attributes: Optional[ExperienceAttributes] = None


class JobApplicationResource(JsonApiResource):
    """Typed Job Application resource."""
    type: str = Field(default="job-application", const=True)
    attributes: Optional[JobApplicationAttributes] = None


class JobApplicationStatusResource(JsonApiResource):
    """Typed Job Application Status resource."""
    type: str = Field(default="job-application-status", const=True)
    attributes: Optional[JobApplicationStatusAttributes] = None


class JobPostResource(JsonApiResource):
    """Typed Job Post resource."""
    type: str = Field(default="job-post", const=True)
    attributes: Optional[JobPostAttributes] = None


class QuestionResource(JsonApiResource):
    """Typed Question resource."""
    type: str = Field(default="question", const=True)
    attributes: Optional[QuestionAttributes] = None


class ResumeResource(JsonApiResource):
    """Typed Resume resource."""
    type: str = Field(default="resume", const=True)
    attributes: Optional[ResumeAttributes] = None


class ScoreResource(JsonApiResource):
    """Typed Score resource."""
    type: str = Field(default="score", const=True)
    attributes: Optional[ScoreAttributes] = None


class ScrapeResource(JsonApiResource):
    """Typed Scrape resource."""
    type: str = Field(default="scrape", const=True)
    attributes: Optional[ScrapeAttributes] = None


class SkillResource(JsonApiResource):
    """Typed Skill resource."""
    type: str = Field(default="skill", const=True)
    attributes: Optional[SkillAttributes] = None


class SummaryResource(JsonApiResource):
    """Typed Summary resource."""
    type: str = Field(default="summary", const=True)
    attributes: Optional[SummaryAttributes] = None


class UserResourceTyped(JsonApiResource):
    """Typed User resource."""
    type: str = Field(default="user", const=True)
    attributes: Optional[UserAttributes] = None
