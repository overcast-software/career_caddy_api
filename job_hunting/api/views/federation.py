"""ActivityPub federation views — WebFinger + Actor.

Phase 5a of Plans/ActivityPub Phase 5 — federation proper. Two root-URL
views (NOT under /api/v1/) — WebFinger lives at the RFC 7033 mandated
``.well-known/webfinger`` prefix, and the Actor view at ``/actors/<u>/``
mirrors the URI shape the Phase 4 ``as_object.actor_uri`` helper has
been emitting since the Phase 4 prep.

The views are deliberately authentication-free. WebFinger is a public
discovery primitive — anything else breaks Mastodon's first contact.
The Actor view is also public; visibility lives at the per-object layer
(Outbox + Follow gates) in 5b/5c.

Lazy keypair generation: the first request that lands on an Actor row
with NULL keys generates an RSA-2048 keypair and persists it under
``SELECT FOR UPDATE`` inside ``transaction.atomic()``. Concurrent
requests block on the row lock instead of racing — verified by the
ThreadPoolExecutor test in ``tests/test_activitypub_phase5a.py``.
"""
from __future__ import annotations

from urllib.parse import unquote, urlparse

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from job_hunting.models import Actor


# RFC 7033 §10.2 — WebFinger MUST serve ``application/jrd+json``.
JRD_CONTENT_TYPE = "application/jrd+json"

# ActivityPub §3.2 — Actor objects served as ``application/activity+json``
# (with ``application/ld+json; profile="https://www.w3.org/ns/activitystreams"``
# as the formal JSON-LD content type Mastodon also accepts).
AS2_CONTENT_TYPE = "application/activity+json"


def _origin() -> str:
    """Return the configured instance origin (no trailing slash)."""
    return settings.INSTANCE_ORIGIN.rstrip("/")


def _origin_host() -> str:
    """Return the host portion of INSTANCE_ORIGIN for WebFinger matching."""
    parsed = urlparse(settings.INSTANCE_ORIGIN)
    # ``netloc`` carries any explicit :port; we keep it intact so the
    # WebFinger ``acct:`` check matches what the client typed.
    return parsed.netloc or parsed.path


def _actor_uri(username: str) -> str:
    """Mint the Actor URI for ``username``. Mirrors as_object.actor_uri
    for local-origin actors but is duplicated here to avoid circular
    imports between federation views and the JobPost adapter."""
    return f"{_origin()}/actors/{username}"


def _ensure_keypair(actor: Actor) -> Actor:
    """Generate + persist an RSA-2048 keypair on ``actor`` if missing.

    ``SELECT FOR UPDATE`` inside ``transaction.atomic()`` serialises
    concurrent requests for the same row — the second waiter re-reads
    after acquiring the lock and sees the keys the first request wrote,
    so generation runs exactly once.
    """
    if actor.has_keypair:
        return actor

    with transaction.atomic():
        locked = Actor.objects.select_for_update().get(pk=actor.pk)
        if locked.has_keypair:
            return locked

        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend(),
        )
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        public_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")

        locked.private_key_pem = private_pem
        locked.public_key_pem = public_pem
        locked.save(update_fields=["private_key_pem", "public_key_pem", "updated_at"])
        return locked


@csrf_exempt
@require_http_methods(["GET"])
def webfinger(request):
    """WebFinger (RFC 7033) — resolve ``acct:<u>@<host>`` to an Actor.

    Mastodon's first contact with any federation peer is a WebFinger
    lookup; without this view Mastodon returns "user not found" and the
    federation chain stops dead. 404 on unknown handles, 400 on
    malformed input.
    """
    resource = request.GET.get("resource", "")
    if not resource.startswith("acct:"):
        return HttpResponseBadRequest("resource must be an acct: URI")

    try:
        acct = unquote(resource[len("acct:"):])
        username, host = acct.split("@", 1)
    except ValueError:
        return HttpResponseBadRequest("resource must be acct:user@host")

    # Strict host check — refuse to advertise local actors under foreign
    # hostnames. The harness can override INSTANCE_ORIGIN to test
    # alternate hosts.
    if host.lower() != _origin_host().lower():
        return JsonResponse({"error": "not found"}, status=404, content_type=JRD_CONTENT_TYPE)

    actor = Actor.objects.filter(preferred_username=username).first()
    if actor is None:
        return JsonResponse({"error": "not found"}, status=404, content_type=JRD_CONTENT_TYPE)

    actor_uri = _actor_uri(actor.preferred_username)
    jrd = {
        "subject": f"acct:{actor.preferred_username}@{_origin_host()}",
        "aliases": [actor_uri],
        "links": [
            {
                "rel": "self",
                "type": AS2_CONTENT_TYPE,
                "href": actor_uri,
            },
            {
                "rel": "http://webfinger.net/rel/profile-page",
                "type": "text/html",
                "href": actor_uri,
            },
        ],
    }
    return JsonResponse(jrd, content_type=JRD_CONTENT_TYPE)


@csrf_exempt
@require_http_methods(["GET"])
def actor_view(request, username: str):
    """Serve the AS2 Person / Service / Application JSON-LD for an Actor.

    Includes the ``publicKey`` block populated from the (lazily-generated)
    RSA keypair so HTTP Signatures verification in Phase 5c has something
    to fetch. The Outbox / Inbox / Followers URIs are emitted as
    placeholders pointing at routes that will exist in 5b/5c — Mastodon
    tolerates 404 on these during initial discovery.
    """
    actor = Actor.objects.filter(preferred_username=username).first()
    if actor is None:
        return JsonResponse(
            {"error": "not found"}, status=404, content_type=AS2_CONTENT_TYPE
        )

    actor = _ensure_keypair(actor)
    actor_uri = _actor_uri(actor.preferred_username)

    body = {
        "@context": [
            "https://www.w3.org/ns/activitystreams",
            "https://w3id.org/security/v1",
        ],
        "id": actor_uri,
        "type": actor.type,
        "preferredUsername": actor.preferred_username,
        "inbox": f"{actor_uri}/inbox",
        "outbox": f"{actor_uri}/outbox",
        "followers": f"{actor_uri}/followers",
        "following": f"{actor_uri}/following",
        "publicKey": {
            "id": f"{actor_uri}#main-key",
            "owner": actor_uri,
            "publicKeyPem": actor.public_key_pem,
        },
    }

    # Optional ``name`` — populated for Person actors from the linked
    # User so Mastodon's search UI shows something readable.
    if actor.user_id and actor.user:
        display = (
            actor.user.get_full_name() or actor.user.username
        )
        if display:
            body["name"] = display

    response = HttpResponse(
        content=JsonResponse(body).content,
        content_type=AS2_CONTENT_TYPE,
    )
    return response


def _empty_collection(request, username: str, collection: str):
    """Shared body for the three Phase 5a-collection stubs.

    Mastodon (and other strict AP peers) enumerate ``outbox`` / ``followers``
    / ``following`` once the Actor JSON references them. If those URIs
    return Django's HTML 404 template, the peer flags the actor as
    broken and refuses subsequent interactions. Returning a valid empty
    ``OrderedCollection`` keeps the actor healthy while the real
    backing models (Phase 5b outbox enumeration + Phase 5c follower
    tracking) stay deferred.

    ``id`` is computed from the path the peer actually requested so the
    peer can verify the response matches the URI it dereferenced —
    catches reverse-proxy rewrites that would otherwise silently
    desync ``id`` from the request URL.
    """
    actor_exists = Actor.objects.filter(preferred_username=username).exists()
    if not actor_exists:
        return JsonResponse(
            {"error": "not found"}, status=404, content_type=AS2_CONTENT_TYPE
        )

    collection_id = f"{_actor_uri(username)}/{collection}"
    body = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": collection_id,
        "type": "OrderedCollection",
        "totalItems": 0,
        "orderedItems": [],
    }
    return HttpResponse(
        content=JsonResponse(body).content,
        content_type=AS2_CONTENT_TYPE,
    )


@csrf_exempt
@require_http_methods(["GET"])
def actor_outbox(request, username: str):
    """Empty ``OrderedCollection`` stub for the Actor's outbox.

    Phase 5b will replace this with a paginated enumeration of public
    ``Create(Note)`` activities derived from JobPost AS2 adapters; for
    now it exists solely so AP peers don't see a broken endpoint when
    they walk the Actor JSON.
    """
    return _empty_collection(request, username, "outbox")


@csrf_exempt
@require_http_methods(["GET"])
def actor_followers(request, username: str):
    """Empty ``OrderedCollection`` stub for the Actor's followers.

    Real follower enumeration lands in Phase 5c alongside the inbox
    Follow handler and the FederationFollower table.
    """
    return _empty_collection(request, username, "followers")


@csrf_exempt
@require_http_methods(["GET"])
def actor_following(request, username: str):
    """Empty ``OrderedCollection`` stub for the Actor's following list.

    We don't track outbound follows yet — kept as an empty collection
    so peer enumeration succeeds rather than 404'ing on the HTML
    debug page.
    """
    return _empty_collection(request, username, "following")
