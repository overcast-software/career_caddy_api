"""
Chat proxy — authenticates the caller, then forwards their credential
to the internal chat service which runs the AI agent.

Uses a raw Django view (not DRF @api_view) because StreamingHttpResponse
must bypass DRF's content negotiation to stream SSE correctly. That choice
has a consequence worth stating up front: because this is not a DRF view,
DEFAULT_AUTHENTICATION_CLASSES does not apply and this module does its OWN
authentication. A credential the rest of the api accepts is not
automatically accepted here — see _authenticate.

Auth pattern (Option C — credential pass-through):
    Client sends a credential → Django validates it → forwards the SAME
    raw credential to the chat service → chat service uses it for
    /api/v1/me/ and all downstream tool calls. No temporary keys minted.

    Two credential shapes, one header (matching the rest of the api):
      - a JWT, from the Ember SPA
      - a `jh_*` API key, from the browser extension / cc_auto / any
        non-SPA client

    Both work downstream unchanged, because DRF lists ApiKeyAuthentication
    first in DEFAULT_AUTHENTICATION_CLASSES, so the same key authenticates
    every hop the chat service makes on the user's behalf.
"""

import json
import logging
import os
import threading
import time
from urllib.parse import urlsplit

import httpx
from django.http import StreamingHttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication

from job_hunting.api.authentication import ApiKeyAuthentication

logger = logging.getLogger(__name__)

CHAT_SERVICE_URL = os.environ.get("CHAT_SERVICE_URL", "http://localhost:8031")

# ID tokens are valid ~1h; refresh a bit early to stay clear of the boundary.
_ID_TOKEN_REFRESH_SKEW_SECONDS = 300
_ID_TOKEN_DEFAULT_TTL_SECONDS = 3600
# Module-level cache: audience -> (id_token, expiry_epoch). Guarded by a lock
# because the streaming view can be entered concurrently.
_id_token_cache: dict[str, tuple[str, float]] = {}
_id_token_lock = threading.Lock()


def _requires_oidc_token(url: str) -> bool:
    """True only for https Cloud Run targets. Localhost / http (local dev,
    `make up-ai`) has no metadata server, so minting would raise — skip it."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        return False
    host = (parts.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return False
    return True


def _fetch_id_token(audience: str) -> str | None:
    """Mint a Google OIDC ID token for `audience` (the chat service base URL).

    Cached per-audience until near expiry. Returns None (logging the failure)
    if minting raises, so the caller falls through to the existing error SSE
    rather than 500-ing.
    """
    now = time.time()
    cached = _id_token_cache.get(audience)
    if cached and cached[1] - _ID_TOKEN_REFRESH_SKEW_SECONDS > now:
        return cached[0]

    with _id_token_lock:
        # Re-check under lock in case another thread just refreshed.
        cached = _id_token_cache.get(audience)
        if cached and cached[1] - _ID_TOKEN_REFRESH_SKEW_SECONDS > now:
            return cached[0]
        try:
            import google.auth.transport.requests
            import google.oauth2.id_token

            auth_req = google.auth.transport.requests.Request()
            token = google.oauth2.id_token.fetch_id_token(auth_req, audience)
        except Exception as e:
            logger.warning("Failed to mint OIDC id token for %s: %s", audience, e)
            return None
        _id_token_cache[audience] = (token, now + _ID_TOKEN_DEFAULT_TTL_SECONDS)
        return token


def _chat_request_headers() -> dict[str, str]:
    """Headers for the outbound chat request. Attaches a Cloud Run service-to-
    service OIDC bearer token for https targets; plain JSON for local dev."""
    headers = {"Content-Type": "application/json"}
    if _requires_oidc_token(CHAT_SERVICE_URL):
        token = _fetch_id_token(CHAT_SERVICE_URL)
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


def _authenticate(request):
    """Authenticate the caller. Returns (user, raw_token) or (None, None).

    This view is raw Django, so DRF's DEFAULT_AUTHENTICATION_CLASSES never
    runs and authentication is this function's job alone. Until BACK-134 it
    tried JWTAuthentication and nothing else, which made /api/v1/chat/ the
    only authenticated endpoint in the api that rejected a valid `jh_` key.

    That was expensive to diagnose, because ApiKeyAuthenticationMiddleware
    has ALREADY run by this point and already set request.user from the key,
    and ApiKeyPermissionMiddleware has already checked its scopes. So
    request.user.is_authenticated was True on the very line that returned
    401, and from a client it read as a permissions problem.

    The raw credential is returned, not just the user, because the chat
    service replays it verbatim on Authorization: Bearer for /api/v1/me/ and
    every downstream tool call.

    NOTE the deliberate narrowing: an API key is accepted on the Bearer
    header ONLY. ApiKeyAuthentication also honours X-API-Key and ?api_key=,
    but those cannot be forwarded — the chat service needs a bearer-shaped
    credential — and Bearer is the documented wire scheme (api/CLAUDE.md:
    "Auth is Bearer, never Api-Key"). Widening this would mean accepting a
    credential here that fails one hop later, which is worse than a 401.
    """
    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    raw_token = auth_header.split(" ", 1)[1] if " " in auth_header else ""

    # API key first, mirroring DEFAULT_AUTHENTICATION_CLASSES ordering
    # (settings.py) so the two paths cannot disagree about precedence.
    if raw_token.startswith("jh_"):
        try:
            result = ApiKeyAuthentication().authenticate(request)
        except AuthenticationFailed:
            # Revoked, expired or unknown key. DRF raises here rather than
            # returning None, and an uncaught DRF exception in a raw Django
            # view is a 500, not a 401.
            return None, None
        if result:
            return result[0], raw_token
        return None, None

    try:
        result = JWTAuthentication().authenticate(request)
        if result:
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

    # THIS DICT IS A WHITELIST, AND THAT IS THE POINT OF FAILURE HERE.
    #
    # Unknown keys in `body` are NOT forwarded. So a new chat request field
    # needs THREE commits in THREE repos — frontend/extension sends it,
    # chat_server consumes it, and this line. The middle one is the one nobody
    # diffs, and it has now been forgotten twice:
    #
    #   - `page_context`, fixed 2026-04-13 in 462195f
    #   - `smart`, shipped on both ends 2026-04-20 and dropped here until
    #     BACK-135. Sixteen weeks of a toggle rendering as engaged
    #     (aria-pressed) while every turn silently ran CHAT_MODEL.
    #
    # If you add a field to the chat request, add it here in the same PR.
    payload = {
        "message": message,
        "token": token,
        "history": body.get("history", []),
        "conversation_id": body.get("conversation_id", ""),
        "page_context": body.get("page_context"),
        "onboarding": body.get("onboarding"),
        # Routes the turn through CHAT_SMART_MODEL (chat_server.py:799-802) and
        # attributes the spend under trigger="chat_smart" (:1134-1144).
        # Coerced rather than passed through: the client controls this value and
        # it only ever reaches a truthiness check downstream, so normalizing it
        # here keeps a stray string out of the spend attribution.
        "smart": bool(body.get("smart")),
    }
    logger.info(
        "Chat proxy page_context: %s, onboarding present: %s",
        payload.get("page_context"),
        payload.get("onboarding") is not None,
    )

    chat_url = f"{CHAT_SERVICE_URL}/chat"
    logger.info("Proxying to chat service at %s", chat_url)

    # Mint the OIDC token outside the generator so a minting failure is logged
    # before streaming begins (the generator still degrades gracefully).
    headers = _chat_request_headers()

    def stream_response():
        try:
            with httpx.Client(timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)) as client:
                with client.stream(
                    "POST",
                    chat_url,
                    json=payload,
                    headers=headers,
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
