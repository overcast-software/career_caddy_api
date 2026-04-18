from django.http import HttpResponse
from drf_spectacular.utils import (
    OpenApiResponse,
    extend_schema,
)
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from job_hunting.lib.services.application_prompt_builder import (
    ApplicationPromptBuilder,
)
from job_hunting.models import CoverLetter, Resume


def _forbidden():
    return Response({"errors": [{"detail": "Forbidden"}]}, status=403)


def _not_found():
    return Response({"errors": [{"detail": "Not found"}]}, status=404)


def _markdown_response(body: str) -> HttpResponse:
    return HttpResponse(body, content_type="text/markdown; charset=utf-8")


@extend_schema(
    tags=["Markdown"],
    summary="Render a resume as markdown (token-efficient view for AI agents)",
    responses={
        200: OpenApiResponse(
            description="text/markdown body",
            response=str,
        ),
        403: OpenApiResponse(description="Forbidden"),
        404: OpenApiResponse(description="Not found"),
    },
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def resume_markdown(request, pk):
    try:
        resume = Resume.objects.select_related("user").get(pk=int(pk))
    except (Resume.DoesNotExist, ValueError):
        return _not_found()

    if resume.user_id != request.user.id and not request.user.is_staff:
        return _forbidden()

    builder = ApplicationPromptBuilder(max_section_chars=60000)
    body = builder._resume_text(resume)
    return _markdown_response(body)


def _cover_letter_markdown(letter: CoverLetter) -> str:
    header_parts = []
    created_at = getattr(letter, "created_at", None)
    if created_at:
        header_parts.append(f"Created: {created_at.strftime('%Y-%m-%d')}")
    if letter.job_post_id and letter.job_post and letter.job_post.title:
        header_parts.append(f"Job: {letter.job_post.title}")
    if letter.company_id and letter.company and letter.company.name:
        header_parts.append(f"Company: {letter.company.name}")
    header = " | ".join(header_parts) if header_parts else "Cover Letter"
    return f"# Cover Letter\n{header}\n\n{letter.content or ''}"


@extend_schema(
    tags=["Markdown"],
    summary="Render a cover letter as markdown",
    responses={
        200: OpenApiResponse(description="text/markdown body", response=str),
        403: OpenApiResponse(description="Forbidden"),
        404: OpenApiResponse(description="Not found"),
    },
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def cover_letter_markdown(request, pk):
    try:
        letter = CoverLetter.objects.select_related("job_post", "company").get(
            pk=int(pk)
        )
    except (CoverLetter.DoesNotExist, ValueError):
        return _not_found()

    if letter.user_id != request.user.id and not request.user.is_staff:
        return _forbidden()

    return _markdown_response(_cover_letter_markdown(letter))
