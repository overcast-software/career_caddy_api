"""
Pydantic-AI agent for interacting with the job hunting API.
Handles authentication and provides methods to interact with various endpoints.
"""

import asyncio
import json
import os
import logging
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urljoin

import httpx
from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext

logger = logging.getLogger(__name__)


class APICredentials(BaseModel):
    """Credentials for API authentication."""

    username: str
    password: str
    base_url: str = "http://localhost:8000"


class APIContext(BaseModel):
    """Context for API operations."""

    model_config = {"arbitrary_types_allowed": True}

    credentials: APICredentials
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    client: Optional[httpx.AsyncClient] = Field(default=None, exclude=True)


class APIResponse(BaseModel):
    """Standardized API response."""

    success: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    status_code: Optional[int] = None


# Create the agent
api_agent = Agent(
    "openai:gpt-4o-mini",
    deps_type=APIContext,
    system_prompt="""
    You are an API client agent for a job hunting application. You can:

    1. Authenticate with the API using JWT tokens
    2. Fetch and manage resumes, job applications, companies, and other resources
    3. Create, update, and delete resources through the API
    4. Handle API errors gracefully

    Always use the provided HTTP client and maintain authentication state.
    Return structured responses with success/error status.
    """,
)


@api_agent.system_prompt
async def add_client_info(ctx: RunContext[APIContext]) -> str:
    """Add current authentication status to system prompt."""
    if ctx.deps.access_token:
        return "You are currently authenticated with a valid access token."
    else:
        return "You are not currently authenticated. You'll need to login first."


@api_agent.tool
async def login(ctx: RunContext[APIContext]) -> APIResponse:
    """Authenticate with the API and obtain JWT tokens."""
    try:
        if not ctx.deps.client:
            ctx.deps.client = httpx.AsyncClient()

        login_url = urljoin(ctx.deps.credentials.base_url, "/api/v1/token/")

        response = await ctx.deps.client.post(
            login_url,
            json={
                "username": ctx.deps.credentials.username,
                "password": ctx.deps.credentials.password,
            },
        )

        if response.status_code == 200:
            tokens = response.json()
            ctx.deps.access_token = tokens.get("access")
            ctx.deps.refresh_token = tokens.get("refresh")

            # Set authorization header for future requests
            ctx.deps.client.headers.update(
                {"Authorization": f"Bearer {ctx.deps.access_token}"}
            )

            logger.info("Successfully authenticated with API")
            return APIResponse(
                success=True,
                data={"message": "Successfully authenticated"},
                status_code=response.status_code,
            )
        else:
            error_msg = f"Authentication failed: {response.status_code}"
            logger.error(error_msg)
            return APIResponse(
                success=False, error=error_msg, status_code=response.status_code
            )

    except Exception as e:
        error_msg = f"Login error: {str(e)}"
        logger.error(error_msg)
        return APIResponse(success=False, error=error_msg)


@api_agent.tool
async def refresh_access_token(ctx: RunContext[APIContext]) -> APIResponse:
    """Refresh the access token using the refresh token."""
    try:
        if not ctx.deps.refresh_token:
            return APIResponse(success=False, error="No refresh token available")

        if not ctx.deps.client:
            ctx.deps.client = httpx.AsyncClient()

        refresh_url = urljoin(ctx.deps.credentials.base_url, "/api/token/refresh/")

        response = await ctx.deps.client.post(
            refresh_url, json={"refresh": ctx.deps.refresh_token}
        )

        if response.status_code == 200:
            tokens = response.json()
            ctx.deps.access_token = tokens.get("access")

            # Update authorization header
            ctx.deps.client.headers.update(
                {"Authorization": f"Bearer {ctx.deps.access_token}"}
            )

            return APIResponse(
                success=True,
                data={"message": "Token refreshed successfully"},
                status_code=response.status_code,
            )
        else:
            return APIResponse(
                success=False,
                error=f"Token refresh failed: {response.status_code}",
                status_code=response.status_code,
            )

    except Exception as e:
        return APIResponse(success=False, error=f"Token refresh error: {str(e)}")


@api_agent.tool
async def get_resumes(ctx: RunContext[APIContext]) -> APIResponse:
    """Fetch all resumes for the authenticated user."""
    try:
        if not ctx.deps.access_token:
            return APIResponse(success=False, error="Not authenticated")

        if not ctx.deps.client:
            return APIResponse(success=False, error="HTTP client not initialized")

        url = urljoin(ctx.deps.credentials.base_url, "/api/v1/resumes/")
        response = await ctx.deps.client.get(url)

        if response.status_code == 200:
            return APIResponse(
                success=True, data=response.json(), status_code=response.status_code
            )
        else:
            return APIResponse(
                success=False,
                error=f"Failed to fetch resumes: {response.status_code}",
                status_code=response.status_code,
            )

    except Exception as e:
        return APIResponse(success=False, error=f"Error fetching resumes: {str(e)}")


@api_agent.tool
async def get_job_applications(ctx: RunContext[APIContext]) -> APIResponse:
    """Fetch all job applications for the authenticated user."""
    try:
        if not ctx.deps.access_token:
            return APIResponse(success=False, error="Not authenticated")

        if not ctx.deps.client:
            return APIResponse(success=False, error="HTTP client not initialized")

        url = urljoin(ctx.deps.credentials.base_url, "/api/v1/job-applications/")
        response = await ctx.deps.client.get(url)

        if response.status_code == 200:
            return APIResponse(
                success=True, data=response.json(), status_code=response.status_code
            )
        else:
            return APIResponse(
                success=False,
                error=f"Failed to fetch job applications: {response.status_code}",
                status_code=response.status_code,
            )

    except Exception as e:
        return APIResponse(
            success=False, error=f"Error fetching job applications: {str(e)}"
        )


@api_agent.tool
async def get_companies(ctx: RunContext[APIContext]) -> APIResponse:
    """Fetch all companies."""
    try:
        if not ctx.deps.access_token:
            return APIResponse(success=False, error="Not authenticated")

        if not ctx.deps.client:
            return APIResponse(success=False, error="HTTP client not initialized")

        url = urljoin(ctx.deps.credentials.base_url, "/api/v1/companies/")
        response = await ctx.deps.client.get(url)

        if response.status_code == 200:
            return APIResponse(
                success=True, data=response.json(), status_code=response.status_code
            )
        else:
            return APIResponse(
                success=False,
                error=f"Failed to fetch companies: {response.status_code}",
                status_code=response.status_code,
            )

    except Exception as e:
        return APIResponse(success=False, error=f"Error fetching companies: {str(e)}")


@api_agent.tool
async def get_favorite_resume(ctx: RunContext[APIContext]) -> APIResponse:
    """Get the user's first/favorite resume."""
    try:
        if not ctx.deps.access_token:
            return APIResponse(success=False, error="Not authenticated")

        if not ctx.deps.client:
            return APIResponse(success=False, error="HTTP client not initialized")

        url = urljoin(ctx.deps.credentials.base_url, "/api/v1/resumes/")
        response = await ctx.deps.client.get(url)

        if response.status_code == 200:
            data = response.json()
            resumes = data.get("data", [])
            if resumes:
                # Return the first resume as the "favorite"
                favorite = resumes[0]
                return APIResponse(
                    success=True, 
                    data={"favorite_resume": favorite}, 
                    status_code=response.status_code
                )
            else:
                return APIResponse(
                    success=False, 
                    error="No resumes found", 
                    status_code=404
                )
        else:
            return APIResponse(
                success=False,
                error=f"Failed to fetch resumes: {response.status_code}",
                status_code=response.status_code,
            )

    except Exception as e:
        return APIResponse(success=False, error=f"Error fetching favorite resume: {str(e)}")


@api_agent.tool
async def create_score(
    ctx: RunContext[APIContext],
    job_post_id: int,
    user_id: int,
    resume_id: Optional[int] = None,
) -> APIResponse:
    """Create a job score. If resume_id is not provided, uses the user's favorite resume."""
    try:
        if not ctx.deps.access_token:
            return APIResponse(success=False, error="Not authenticated")

        if not ctx.deps.client:
            return APIResponse(success=False, error="HTTP client not initialized")

        # If no resume_id provided, get the favorite resume
        if resume_id is None:
            favorite_response = await get_favorite_resume(ctx)
            if not favorite_response.success:
                return APIResponse(
                    success=False, 
                    error=f"Could not get favorite resume: {favorite_response.error}"
                )
            
            favorite_resume = favorite_response.data.get("favorite_resume")
            if not favorite_resume:
                return APIResponse(
                    success=False, 
                    error="No favorite resume available"
                )
            
            resume_id = int(favorite_resume.get("id"))
            
            # Confirm with user about using favorite resume
            resume_title = favorite_resume.get("attributes", {}).get("title", "Untitled")
            confirmation_msg = f"No resume specified. Using your favorite resume: '{resume_title}' (ID: {resume_id}). Proceeding with score creation."
            logger.info(confirmation_msg)

        url = urljoin(ctx.deps.credentials.base_url, "/api/v1/scores/")

        payload = {
            "data": {
                "type": "score",
                "attributes": {},
                "relationships": {
                    "job-post": {"data": {"type": "job-post", "id": str(job_post_id)}},
                    "user": {"data": {"type": "user", "id": str(user_id)}},
                    "resume": {"data": {"type": "resume", "id": str(resume_id)}},
                },
            }
        }

        response = await ctx.deps.client.post(url, json=payload)

        if response.status_code in [200, 201]:
            return APIResponse(
                success=True, data=response.json(), status_code=response.status_code
            )
        else:
            return APIResponse(
                success=False,
                error=f"Failed to create score: {response.status_code}",
                status_code=response.status_code,
            )

    except Exception as e:
        return APIResponse(
            success=False, error=f"Error creating score: {str(e)}"
        )


@api_agent.tool
async def create_scrape(
    ctx: RunContext[APIContext],
    url: str,
) -> APIResponse:
    """Create a scrape request for the given URL."""
    try:
        if not ctx.deps.access_token:
            return APIResponse(success=False, error="Not authenticated")

        if not ctx.deps.client:
            return APIResponse(success=False, error="HTTP client not initialized")

        api_url = urljoin(ctx.deps.credentials.base_url, "/api/v1/scrapes/")

        payload = {
            "data": {
                "type": "scrape",
                "attributes": {
                    "url": url
                }
            }
        }

        response = await ctx.deps.client.post(api_url, json=payload)

        if response.status_code in [200, 201, 202]:
            return APIResponse(
                success=True, data=response.json(), status_code=response.status_code
            )
        else:
            return APIResponse(
                success=False,
                error=f"Failed to create scrape: {response.status_code}",
                status_code=response.status_code,
            )

    except Exception as e:
        return APIResponse(
            success=False, error=f"Error creating scrape: {str(e)}"
        )


@api_agent.tool
async def get_job_posts(ctx: RunContext[APIContext]) -> APIResponse:
    """Fetch all job posts."""
    try:
        if not ctx.deps.access_token:
            return APIResponse(success=False, error="Not authenticated")

        if not ctx.deps.client:
            return APIResponse(success=False, error="HTTP client not initialized")

        url = urljoin(ctx.deps.credentials.base_url, "/api/v1/job-posts/")
        response = await ctx.deps.client.get(url)

        if response.status_code == 200:
            return APIResponse(
                success=True, data=response.json(), status_code=response.status_code
            )
        else:
            return APIResponse(
                success=False,
                error=f"Failed to fetch job posts: {response.status_code}",
                status_code=response.status_code,
            )

    except Exception as e:
        return APIResponse(success=False, error=f"Error fetching job posts: {str(e)}")


@api_agent.tool
async def create_job_application(
    ctx: RunContext[APIContext],
    job_post_id: int,
    resume_id: int,
    cover_letter_id: Optional[int] = None,
) -> APIResponse:
    """Create a new job application."""
    try:
        if not ctx.deps.access_token:
            return APIResponse(success=False, error="Not authenticated")

        if not ctx.deps.client:
            return APIResponse(success=False, error="HTTP client not initialized")

        url = urljoin(ctx.deps.credentials.base_url, "/api/v1/job-applications/")

        payload = {
            "data": {
                "type": "job-application",
                "attributes": {},
                "relationships": {
                    "job-post": {"data": {"type": "job-post", "id": str(job_post_id)}},
                    "resume": {"data": {"type": "resume", "id": str(resume_id)}},
                },
            }
        }

        if cover_letter_id:
            payload["data"]["relationships"]["cover-letter"] = {
                "data": {"type": "cover-letter", "id": str(cover_letter_id)}
            }

        response = await ctx.deps.client.post(url, json=payload)

        if response.status_code in [200, 201]:
            return APIResponse(
                success=True, data=response.json(), status_code=response.status_code
            )
        else:
            return APIResponse(
                success=False,
                error=f"Failed to create job application: {response.status_code}",
                status_code=response.status_code,
            )

    except Exception as e:
        return APIResponse(
            success=False, error=f"Error creating job application: {str(e)}"
        )


@api_agent.tool
async def api_request(
    ctx: RunContext[APIContext],
    method: str,
    endpoint: str,
    data: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> APIResponse:
    """Make a generic API request."""
    try:
        if not ctx.deps.access_token:
            return APIResponse(success=False, error="Not authenticated")

        if not ctx.deps.client:
            return APIResponse(success=False, error="HTTP client not initialized")

        url = urljoin(ctx.deps.credentials.base_url, endpoint)

        kwargs = {}
        if data:
            kwargs["json"] = data
        if params:
            kwargs["params"] = params

        response = await ctx.deps.client.request(method.upper(), url, **kwargs)

        try:
            response_data = response.json()
        except:
            response_data = {"text": response.text}

        return APIResponse(
            success=response.status_code < 400,
            data=response_data,
            status_code=response.status_code,
        )

    except Exception as e:
        return APIResponse(success=False, error=f"API request error: {str(e)}")


async def cleanup_client(ctx: APIContext):
    """Clean up the HTTP client."""
    if ctx.client:
        await ctx.client.aclose()


# Example usage function
async def example_usage():
    """Example of how to use the API agent."""
    credentials = APICredentials(
        username=os.environ.get("USERNAME"),
        password=os.environ.get("PASSWORD"),
        base_url=os.environ.get("APP_URL", "http://localhost:8000"),
    )

    context = APIContext(credentials=credentials)

    try:
        # Login
        result = await api_agent.run("Please login to the API", deps=context)
        print(f"Login result: {result.output}")

        # Ask user for URL to scrape
        url = input("Please enter a job posting URL to scrape: ").strip()
        if not url:
            print("No URL provided, skipping scrape example")
            return

        # Create scrape request using the API
        result = await api_agent.run(
            f"Create a scrape for the URL: {url}",
            deps=context
        )
        print(f"Scrape creation result: {result.output}")

        # Wait a moment for processing
        import asyncio
        await asyncio.sleep(2)

        # Get job posts to find the newly created one
        result = await api_agent.run("Get all job posts", deps=context)
        print(f"Job posts: {result.output}")
        
        # Extract job post ID from the response (assuming the newest one is what we want)
        job_post_id = None
        try:
            # Parse the response to find job post ID
            # This is a simplified approach - in practice you'd want more robust parsing
            if "data" in str(result.output):
                # Look for job post ID in the response
                import re
                id_matches = re.findall(r'"id":\s*"?(\d+)"?', str(result.output))
                if id_matches:
                    job_post_id = int(id_matches[-1])  # Get the last (newest) one
        except Exception as e:
            print(f"Error parsing job post ID: {e}")

        if job_post_id:
            print(f"Found job post ID: {job_post_id}")
            
            # Get user ID (assuming user ID 1 for this example)
            user_id = 1
            
            # Create a score using the job post and favorite resume (resume_id will be auto-selected)
            result = await api_agent.run(
                f"Create a score for job post ID {job_post_id} and user ID {user_id}",
                deps=context
            )
            print(f"Score creation result: {result.output}")
        else:
            print("Could not find job post ID from scrape result")

    finally:
        await cleanup_client(context)


if __name__ == "__main__":
    asyncio.run(example_usage())
