from drf_spectacular.utils import (
    OpenApiResponse,
    inline_serializer,
    OpenApiParameter,
)
from drf_spectacular.types import OpenApiTypes
from rest_framework import serializers as drf_serializers

_INCLUDE_PARAM = OpenApiParameter(
    "include",
    OpenApiTypes.STR,
    OpenApiParameter.QUERY,
    required=False,
    description="Comma-separated relationships to sideload (e.g. resumes,api-keys).",
)

_PAGE_PARAMS = [
    OpenApiParameter(
        "page[number]",
        OpenApiTypes.INT,
        OpenApiParameter.QUERY,
        required=False,
        description="Page number (1-based). Default: 1.",
    ),
    OpenApiParameter(
        "page[size]",
        OpenApiTypes.INT,
        OpenApiParameter.QUERY,
        required=False,
        description="Items per page. Default: 50.",
    ),
    _INCLUDE_PARAM,
]

_SORT_PARAM = OpenApiParameter(
    "sort",
    OpenApiTypes.STR,
    OpenApiParameter.QUERY,
    required=False,
    description="Comma-separated sort fields. Prefix with '-' for descending (e.g., '-created_at' for newest first).",
)

_FILTER_QUERY_PARAM = OpenApiParameter(
    "filter[query]",
    OpenApiTypes.STR,
    OpenApiParameter.QUERY,
    required=False,
    description="Search across title, description, company name, and company display_name (case-insensitive OR).",
)
_FILTER_COMPANY_PARAM = OpenApiParameter(
    "filter[company]",
    OpenApiTypes.STR,
    OpenApiParameter.QUERY,
    required=False,
    description="Filter by company name (case-insensitive contains).",
)
_FILTER_COMPANY_ID_PARAM = OpenApiParameter(
    "filter[company_id]",
    OpenApiTypes.INT,
    OpenApiParameter.QUERY,
    required=False,
    description="Filter by exact company ID.",
)
_FILTER_TITLE_PARAM = OpenApiParameter(
    "filter[title]",
    OpenApiTypes.STR,
    OpenApiParameter.QUERY,
    required=False,
    description="Filter by job post title (case-insensitive contains).",
)

_FILTER_APP_QUERY_PARAM = OpenApiParameter(
    "filter[query]",
    OpenApiTypes.STR,
    OpenApiParameter.QUERY,
    required=False,
    description="Search across job post title, company name, company display_name, status, and notes (case-insensitive OR).",
)
_FILTER_APP_STATUS_PARAM = OpenApiParameter(
    "filter[status]",
    OpenApiTypes.STR,
    OpenApiParameter.QUERY,
    required=False,
    description="Filter by application status (case-insensitive contains).",
)

_JSONAPI_LIST = OpenApiResponse(
    description="JSON:API list",
    response=inline_serializer(
        name="JsonApiList",
        fields={
            "data": drf_serializers.ListField(child=drf_serializers.DictField()),
            "included": drf_serializers.ListField(
                child=drf_serializers.DictField(), required=False
            ),
        },
    ),
)
_JSONAPI_ITEM = OpenApiResponse(
    description="JSON:API resource",
    response=inline_serializer(
        name="JsonApiItem",
        fields={
            "data": drf_serializers.DictField(),
            "included": drf_serializers.ListField(
                child=drf_serializers.DictField(), required=False
            ),
        },
    ),
)
_JSONAPI_WRITE = inline_serializer(
    name="JsonApiWrite",
    fields={
        "data": drf_serializers.DictField(
            help_text="JSON:API resource object with 'type', 'attributes', and optional 'relationships'."
        )
    },
)
