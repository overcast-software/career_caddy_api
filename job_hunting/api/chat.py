"""
Chat proxy — authenticates the user via JWT, then forwards the token
to the internal chat service which runs the AI agent.

Uses a raw Django view (not DRF @api_view) because StreamingHttpResponse
must bypass DRF's content negotiation to stream SSE correctly.

Auth pattern (Option C — JWT pass-through):
    Frontend sends JWT → Django validates it → forwards the same JWT to
    the chat service → chat service uses it for /api/v1/me/ profile fetch
    and all downstream tool calls. No temporary API keys created.
"""

import json
import logging
import os

import httpx
from django.http import StreamingHttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework_simplejwt.authentication import JWTAuthentication

logger = logging.getLogger(__name__)

CHAT_SERVICE_URL = os.environ.get("CHAT_SERVICE_URL", "http://localhost:8031")


def _authenticate(request):
    """Authenticate via JWT. Returns (user, raw_token) or (None, None)."""
    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    jwt_auth = JWTAuthentication()
    try:
        result = jwt_auth.authenticate(request)
        if result:
            # Extract the raw token string from the Authorization header
            raw_token = auth_header.split(" ", 1)[1] if " " in auth_header else ""
            return result[0], raw_token
    except Exception:
        pass
    return None, None


@csrf_exempt
def chat_proxy(request):
    """POST /api/v1/chat/ — proxy chat to the internal chat service."""
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    user, token = _authenticate(request)
    if not user or not token:
        return JsonResponse({"error": "Authentication required"}, status=401)

    logger.info("Chat request from user=%s", user.id)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    message = (body.get("message") or "").strip()
    if not message:
        return JsonResponse({"error": "message is required"}, status=400)

    payload = {
        "message": message,
        "token": token,
        "history": body.get("history", []),
        "conversation_id": body.get("conversation_id", ""),
        "page_context": body.get("page_context"),
        "onboarding": body.get("onboarding"),
    }
    logger.info(
        "Chat proxy page_context: %s, onboarding present: %s",
        payload.get("page_context"),
        payload.get("onboarding") is not None,
    )

    chat_url = f"{CHAT_SERVICE_URL}/chat"
    logger.info("Proxying to chat service at %s", chat_url)

    def stream_response():
        try:
            with httpx.Client(timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)) as client:
                with client.stream(
                    "POST",
                    chat_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    if resp.status_code != 200:
                        error = json.dumps({
                            "type": "error",
                            "content": f"Chat service returned {resp.status_code}",
                        })
                        yield f"data: {error}\n\n"
                        return

                    for line in resp.iter_lines():
                        if line:
                            yield f"{line}\n\n"
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            logger.warning("Chat service unavailable: %s", e)
            error = json.dumps({
                "type": "error",
                "content": "Chat service is unavailable",
            })
            yield f"data: {error}\n\n"
        except httpx.RemoteProtocolError as e:
            logger.warning("Chat service closed connection: %s", e)
            error = json.dumps({
                "type": "error",
                "content": "Chat service closed the connection unexpectedly",
            })
            yield f"data: {error}\n\n"
        except Exception as e:
            logger.exception("Chat proxy error")
            error = json.dumps({"type": "error", "content": str(e)})
            yield f"data: {error}\n\n"

    response = StreamingHttpResponse(
        stream_response(),
        content_type="text/event-stream",
    )
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response
