"""ActivityPub federation views — WebFinger + Actor + Inbox.

Phase 5a/5b/5c of Plans/ActivityPub Phase 5 — federation proper.
Root-URL views (NOT under /api/v1/) — WebFinger lives at the RFC 7033
mandated ``.well-known/webfinger`` prefix, and the Actor / Outbox /
Followers / Following / Inbox URIs all hang off ``/actors/<u>/``.

Auth model: WebFinger + Actor + collections are unauthenticated
(federation peers have no auth context on first contact). The inbox
is HTTP-Signature-authenticated — every POST proves its actor identity
cryptographically, so the inbox path skips Django auth + DRF
permissions and trusts the verified ``keyId`` instead.

Lazy keypair generation: the first request that lands on an Actor row
with NULL keys generates an RSA-2048 keypair and persists it under
``SELECT FOR UPDATE`` inside ``transaction.atomic()``. Concurrent
requests block on the row lock instead of racing — verified by the
ThreadPoolExecutor test in ``tests/test_activitypub_phase5a.py``.
"""
from __future__ import annotations

import json
import logging
import uuid
from urllib.parse import unquote, urlparse

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from job_hunting.lib.as_object import build_create_activity_for_jobpost
from job_hunting.lib import federation_signing
from job_hunting.lib import federation_ingest
from job_hunting.models import (
    Actor,
    FederationActivity,
    FederationFollower,
    JobPost,
)
from job_hunting.models.federation_activity import (
    ACTIVITY_TYPE_ACCEPT,
    ACTIVITY_TYPE_CREATE,
    ACTIVITY_TYPE_DELETE,
    ACTIVITY_TYPE_FOLLOW,
    ACTIVITY_TYPE_OTHER,
    ACTIVITY_TYPE_UNDO,
    DELIVERY_ACCEPTED,
    DELIVERY_FAILED,
    DIRECTION_INBOUND,
    DIRECTION_OUTBOUND,
)
from job_hunting.models.job_post import AS2_PUBLIC


logger = logging.getLogger(__name__)


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


def _outbox_jobpost_queryset(actor: Actor):
    """Return the actor's public-audience JobPosts in outbox order.

    Filter: ``audience`` contains the AS2 Public URI AND
    ``created_by_id == actor.user_id``. Posts with empty / private
    audiences are excluded by definition — they were never federable.
    When ``actor.user_id is None`` (the future instance-actor case)
    the queryset is empty so the outbox renders as zero items rather
    than 500'ing on the absent owner.

    Sort: ``-created_at`` then ``-id`` so identical creation timestamps
    (paste-storms, demo seed) have a stable secondary order — important
    once peers diff pages between requests.
    """
    if actor.user_id is None:
        return JobPost.objects.none()
    return (
        JobPost.objects.filter(
            created_by_id=actor.user_id,
            audience__contains=[AS2_PUBLIC],
        )
        .order_by("-created_at", "-id")
    )


def _outbox_page_count(total: int, page_size: int) -> int:
    """Number of pages needed to enumerate ``total`` items.

    Zero items → zero pages (callers should suppress ``first`` / ``last``
    in that case so the metadata doesn't advertise a non-existent
    ``/outbox?page=1``).
    """
    if total <= 0:
        return 0
    return (total + page_size - 1) // page_size


@csrf_exempt
@require_http_methods(["GET"])
def actor_outbox(request, username: str):
    """Paginated ``OrderedCollection`` of the actor's public Create(Note) activities.

    Phase 5b — the read-only outbox. No ``page`` query → metadata-only
    OrderedCollection advertising ``totalItems``, ``first``, ``last``.
    ``?page=N`` → an OrderedCollectionPage with up to
    ``ACTIVITYPUB_OUTBOX_PAGE_SIZE`` Create activities plus ``next`` /
    ``prev`` / ``partOf`` links. The Create envelope is built fresh per
    request by ``build_create_activity_for_jobpost``; no Activity rows
    are persisted (5d's territory) — UUIDs are derived deterministically
    from the JobPost id so peers caching by ``id`` see stable
    identifiers across requests.
    """
    actor = Actor.objects.filter(preferred_username=username).first()
    if actor is None:
        return JsonResponse(
            {"error": "not found"}, status=404, content_type=AS2_CONTENT_TYPE
        )

    collection_id = f"{_actor_uri(username)}/outbox"
    page_size = getattr(settings, "ACTIVITYPUB_OUTBOX_PAGE_SIZE", 20)
    queryset = _outbox_jobpost_queryset(actor)
    total = queryset.count()
    last_page = _outbox_page_count(total, page_size)

    page_param = request.GET.get("page")
    if page_param is None:
        body = {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": collection_id,
            "type": "OrderedCollection",
            "totalItems": total,
        }
        if last_page > 0:
            body["first"] = f"{collection_id}?page=1"
            body["last"] = f"{collection_id}?page={last_page}"
        return HttpResponse(
            content=JsonResponse(body).content,
            content_type=AS2_CONTENT_TYPE,
        )

    try:
        page = int(page_param)
    except (TypeError, ValueError):
        return JsonResponse(
            {"error": "invalid page"}, status=404, content_type=AS2_CONTENT_TYPE
        )
    if page < 1 or page > last_page:
        # Out-of-range: page=0 / negative / past the end / any page
        # against an empty collection. The metadata body suppresses
        # ``first`` when the collection is empty, so peers shouldn't be
        # guessing at page URIs anyway.
        return JsonResponse(
            {"error": "page out of range"},
            status=404,
            content_type=AS2_CONTENT_TYPE,
        )

    offset = (page - 1) * page_size
    items = [
        build_create_activity_for_jobpost(job_post, actor)
        for job_post in queryset[offset : offset + page_size]
    ]

    body = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{collection_id}?page={page}",
        "type": "OrderedCollectionPage",
        "partOf": collection_id,
        "orderedItems": items,
    }
    if page < last_page:
        body["next"] = f"{collection_id}?page={page + 1}"
    if page > 1:
        body["prev"] = f"{collection_id}?page={page - 1}"

    return HttpResponse(
        content=JsonResponse(body).content,
        content_type=AS2_CONTENT_TYPE,
    )


def _followers_queryset(actor: Actor):
    """Return active ``FederationFollower`` rows for ``actor``.

    Filter: ``local_user_id == actor.user_id`` AND
    ``unfollowed_at IS NULL`` (Undo'd rows stay in the table for audit /
    re-follow detection but don't enumerate as current followers).
    Returns an empty queryset when ``actor.user_id`` is None (instance
    actor doesn't carry per-user followers in V1).

    Sort: ``-accepted_at, -created_at, -id`` — accepted rows first so
    Mastodon's UI sees confirmed followers ahead of the (rare)
    pending-Accept stragglers. ``-id`` is the deterministic tiebreaker
    once timestamps match.
    """
    if actor.user_id is None:
        return FederationFollower.objects.none()
    return (
        FederationFollower.objects.filter(
            local_user_id=actor.user_id,
            unfollowed_at__isnull=True,
        )
        .order_by("-accepted_at", "-created_at", "-id")
    )


@csrf_exempt
@require_http_methods(["GET"])
def actor_followers(request, username: str):
    """Paginated ``OrderedCollection`` of the Actor's active followers.

    Phase 5c — real follower enumeration backed by ``FederationFollower``.
    Shape mirrors the Phase 5b outbox:
    metadata-only ``OrderedCollection`` (no ``page``) advertises
    ``totalItems`` + ``first`` / ``last``; ``?page=N`` returns an
    ``OrderedCollectionPage`` of ``actor_uri`` strings (per AS2 spec —
    followers collection items are bare URIs, not full activities).
    """
    actor = Actor.objects.filter(preferred_username=username).first()
    if actor is None:
        return JsonResponse(
            {"error": "not found"}, status=404, content_type=AS2_CONTENT_TYPE
        )

    collection_id = f"{_actor_uri(username)}/followers"
    page_size = getattr(settings, "ACTIVITYPUB_OUTBOX_PAGE_SIZE", 20)
    queryset = _followers_queryset(actor)
    total = queryset.count()
    last_page = _outbox_page_count(total, page_size)

    page_param = request.GET.get("page")
    if page_param is None:
        body = {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": collection_id,
            "type": "OrderedCollection",
            "totalItems": total,
        }
        if last_page > 0:
            body["first"] = f"{collection_id}?page=1"
            body["last"] = f"{collection_id}?page={last_page}"
        return HttpResponse(
            content=JsonResponse(body).content,
            content_type=AS2_CONTENT_TYPE,
        )

    try:
        page = int(page_param)
    except (TypeError, ValueError):
        return JsonResponse(
            {"error": "invalid page"}, status=404, content_type=AS2_CONTENT_TYPE
        )
    if page < 1 or page > last_page:
        return JsonResponse(
            {"error": "page out of range"},
            status=404,
            content_type=AS2_CONTENT_TYPE,
        )

    offset = (page - 1) * page_size
    items = [
        follower.actor_uri
        for follower in queryset[offset : offset + page_size]
    ]

    body = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": f"{collection_id}?page={page}",
        "type": "OrderedCollectionPage",
        "partOf": collection_id,
        "orderedItems": items,
    }
    if page < last_page:
        body["next"] = f"{collection_id}?page={page + 1}"
    if page > 1:
        body["prev"] = f"{collection_id}?page={page - 1}"

    return HttpResponse(
        content=JsonResponse(body).content,
        content_type=AS2_CONTENT_TYPE,
    )


@csrf_exempt
@require_http_methods(["GET"])
def actor_following(request, username: str):
    """Empty ``OrderedCollection`` stub for the Actor's following list.

    V1 doesn't follow remote actors (5d+); kept as an empty collection
    so peer enumeration succeeds rather than 404'ing on the HTML
    debug page.
    """
    return _empty_collection(request, username, "following")


# ---------------------------------------------------------------------------
# Phase 5c — Inbox.
#
# Pre-flight order (BAIL on any failure):
#   1. Body parse (400 on malformed / too large)
#   2. Activity type sniff (fall through to 202 for forward-compat types)
#   3. Date header window (401 on stale)
#   4. HTTP Signature verification (401 on mismatch / missing)
#   5. Digest header match (401 on mismatch)
#   6. Per-instance rate limit (429 on bucket full)
#   7. Replay dedupe via FederationActivity unique (silent 202 on dupe)
#
# After verification, dispatch by activity type:
#   - Follow → upsert FederationFollower + queue Accept(Follow)
#   - Undo(Follow) → set unfollowed_at
#   - Create(Note) → log only; 5e ingest is deferred
#   - * → log as Other; 202
#
# TODO(5c+): hook for instance allowlist / blocklist before signature
# verification — cheaper to reject by Host header than to verify a sig
# from a banned instance.


def _inbox_error(verdict: str, status: int) -> JsonResponse:
    """Uniform error response. Mastodon's debug UI shows the body verbatim."""
    return JsonResponse(
        {"error": verdict}, status=status, content_type=AS2_CONTENT_TYPE
    )


def _rate_limit_check(host: str) -> bool:
    """Per-instance sliding-window rate limit using Django's cache backend.

    Bucket key is ``ap:rl:<host>:<hour-int>``. We increment-or-create and
    compare against ``ACTIVITYPUB_INBOX_RATE_LIMIT_PER_HOUR``. The hour
    bucket rolls over wall-clock; that's coarser than a true sliding
    window but vastly simpler and the practical fediverse rate is
    nowhere near the 1000/hour default. Returns True if request should
    proceed, False if it's over the limit.

    Per-instance (not per-IP) keeps the blast radius bounded to a
    single peer instance: a misbehaving Mastodon server can't grief
    the global limit for other peers behind the same edge.
    """
    if not host:
        # Refuse to rate-limit on an empty host bucket — that would
        # let unsigned / partially-parsed requests share a global
        # counter. Let them through here; the signature step will
        # reject them.
        return True
    limit = getattr(settings, "ACTIVITYPUB_INBOX_RATE_LIMIT_PER_HOUR", 1000)
    hour = int(timezone.now().timestamp() // 3600)
    key = f"ap:rl:{host}:{hour}"
    try:
        # Django cache: add() is atomic-ish; incr() raises if key absent.
        # Pattern: try incr, fall back to add+1 on miss.
        current = cache.incr(key)
    except ValueError:
        cache.set(key, 1, 3700)
        current = 1
    return current <= limit


def _deliver_accept(follow_activity: dict, follower: FederationFollower,
                    actor: Actor) -> FederationActivity:
    """Build, sign, and POST an Accept(Follow); persist outbound row.

    On peer 2xx: set FederationFollower.accepted_at + outbound row's
    delivered_at. On peer error: delivery_status=failed, error stored.
    Returns the outbound FederationActivity row either way.
    """
    actor_uri_str = _actor_uri(actor.preferred_username)
    accept_id = f"{_origin()}/activities/{uuid.uuid4()}"
    accept_body = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "id": accept_id,
        "type": "Accept",
        "actor": actor_uri_str,
        "object": follow_activity,
    }
    body_bytes = json.dumps(accept_body, separators=(",", ":")).encode("utf-8")

    outbound = FederationActivity.objects.create(
        direction=DIRECTION_OUTBOUND,
        activity_type=ACTIVITY_TYPE_ACCEPT,
        activity_id=accept_id,
        actor_uri=actor_uri_str,
        target_uri=follower.actor_uri,
        local_user_id=actor.user_id,
        body=json.dumps(accept_body),
        delivery_status="pending",
    )

    status_code, snippet = federation_signing.deliver(
        follower.inbox_uri, body_bytes, actor
    )
    if 200 <= status_code < 300:
        now = timezone.now()
        outbound.delivery_status = DELIVERY_ACCEPTED
        outbound.delivered_at = now
        outbound.save(update_fields=["delivery_status", "delivered_at"])
        FederationFollower.objects.filter(pk=follower.pk).update(accepted_at=now)
    else:
        outbound.delivery_status = DELIVERY_FAILED
        outbound.delivery_error = f"status={status_code} body={snippet}"
        outbound.save(update_fields=["delivery_status", "delivery_error"])
    return outbound


def _peer_actor_endpoints(actor_uri: str) -> tuple[str, str | None]:
    """Fetch the remote actor JSON and return (inbox_uri, shared_inbox_uri).

    Falls back gracefully — if endpoints are missing, returns
    ``(actor_uri + '/inbox', None)`` as a best-effort default rather
    than failing the Follow handshake outright. Real-world fediverse
    actors always populate ``inbox``; the fallback only kicks in for
    badly-formed peers we still want to be civil to.
    """
    timeout = getattr(settings, "ACTIVITYPUB_OUTBOUND_DELIVERY_TIMEOUT", 10)
    import requests  # local import to keep view-load fast
    try:
        response = requests.get(
            actor_uri,
            headers={"Accept": AS2_CONTENT_TYPE},
            timeout=timeout,
        )
        if response.status_code != 200:
            return f"{actor_uri.rstrip('/')}/inbox", None
        data = response.json()
    except (requests.RequestException, ValueError):
        return f"{actor_uri.rstrip('/')}/inbox", None
    inbox = data.get("inbox") or f"{actor_uri.rstrip('/')}/inbox"
    shared = (data.get("endpoints") or {}).get("sharedInbox")
    return inbox, shared


def _handle_follow(activity: dict, actor: Actor,
                   verified: federation_signing.VerifiedSignature) -> JsonResponse:
    """Process a Follow activity: upsert follower + dispatch Accept."""
    actor_uri_local = _actor_uri(actor.preferred_username)
    target = activity.get("object")
    if target != actor_uri_local:
        return _inbox_error("follow_target_mismatch", 422)

    follower_actor_uri = activity.get("actor") or verified.actor_uri
    if not follower_actor_uri:
        return _inbox_error("missing_actor", 422)

    inbox_uri, shared_inbox_uri = _peer_actor_endpoints(follower_actor_uri)
    host = FederationFollower.host_for_uri(follower_actor_uri)

    follower, created = FederationFollower.objects.update_or_create(
        local_user_id=actor.user_id,
        actor_uri=follower_actor_uri,
        defaults={
            "inbox_uri": inbox_uri,
            "shared_inbox_uri": shared_inbox_uri,
            "instance_host": host,
            "unfollowed_at": None,  # re-follow case: clear any prior Undo
        },
    )

    _log_inbound(activity, actor, verified, ACTIVITY_TYPE_FOLLOW, target)

    # Synchronous Accept dispatch — V1 one-shot. Failures are logged on
    # the outbound row; the 5d dispatcher will replay them.
    _deliver_accept(activity, follower, actor)

    return JsonResponse(
        {"status": "accepted"},
        status=202,
        content_type=AS2_CONTENT_TYPE,
    )


def _handle_undo(activity: dict, actor: Actor,
                 verified: federation_signing.VerifiedSignature) -> JsonResponse:
    """Process an Undo(Follow): set unfollowed_at on the follower row.

    Other Undo subtypes (Undo(Like), Undo(Announce)) fall through to
    the Other bucket — V1 only models Follow, so an Undo against
    something we never tracked is a no-op (404 with the audit row
    written so 5e replay still sees it).
    """
    inner = activity.get("object") or {}
    if not isinstance(inner, dict):
        return _inbox_error("undo_object_not_object", 422)
    if inner.get("type") != "Follow":
        # Forward-compat: log + 202, don't 422 a peer for Undo(Like) etc.
        _log_inbound(activity, actor, verified, ACTIVITY_TYPE_OTHER, None)
        return JsonResponse(
            {"status": "accepted"}, status=202, content_type=AS2_CONTENT_TYPE
        )

    actor_uri_local = _actor_uri(actor.preferred_username)
    if inner.get("object") != actor_uri_local:
        return _inbox_error("undo_target_mismatch", 422)

    follower_actor_uri = inner.get("actor") or verified.actor_uri
    row = FederationFollower.objects.filter(
        local_user_id=actor.user_id,
        actor_uri=follower_actor_uri,
    ).first()
    if row is None:
        # Still log it so the audit trail captures the (rare) case of a
        # peer sending Undo without our ever having an active row.
        _log_inbound(activity, actor, verified, ACTIVITY_TYPE_UNDO, actor_uri_local)
        return _inbox_error("not_following", 404)

    FederationFollower.objects.filter(pk=row.pk).update(unfollowed_at=timezone.now())
    _log_inbound(activity, actor, verified, ACTIVITY_TYPE_UNDO, actor_uri_local)

    return JsonResponse(
        {"status": "accepted"}, status=202, content_type=AS2_CONTENT_TYPE
    )


def _handle_create(activity: dict, actor: Actor,
                   verified: federation_signing.VerifiedSignature) -> JsonResponse:
    """Process a Create activity: log + 5e ingest.

    5c logs the audit row; 5e turns the Note into a JobPost (or merges
    into an existing one). The spec requires us to return 202 to the
    peer regardless of the ingest outcome — the activity verified, so
    we accepted it. Internal disposition (created / merged / rejected
    / skipped) lives on the FederationActivity row's
    ``delivery_status`` field for the operator + the dedup-feedback
    report.

    Kill-switch: when ``ACTIVITYPUB_INGEST_ENABLED`` is False the
    activity is still logged (so post-toggle replay via
    ``replay_inbound_creates`` can catch up) but no JobPost is touched.
    """
    inner = activity.get("object") or {}
    target = inner.get("id") if isinstance(inner, dict) else None
    audit_row = _log_inbound(activity, actor, verified, ACTIVITY_TYPE_CREATE, target)

    if getattr(settings, "ACTIVITYPUB_INGEST_ENABLED", True):
        # audit_row may be None if the unique (direction, activity_id,
        # target_uri) constraint short-circuited as a replay. In that
        # case we DON'T re-ingest — the original logged-row's outcome
        # already represents the activity. The 202 return below is the
        # idempotent reply the peer expects.
        if audit_row is not None:
            federation_ingest.ingest_create_note(activity, federation_activity=audit_row)

    return JsonResponse(
        {"status": "accepted"}, status=202, content_type=AS2_CONTENT_TYPE
    )


def _resolve_jobpost_from_object(target: object) -> JobPost | None:
    """Resolve a JobPost row from an inbound `Delete` activity's object.

    The `Delete` envelope's ``object`` is usually a bare URI string (the
    AS2 tombstone shape Mastodon emits) but can also be a dict carrying
    ``id`` or ``id`` + ``type: "Tombstone"``. Both shapes resolve to the
    same JobPost via the trailing ``/job-posts/<pk>`` segment of the
    URI; the URI's host is checked separately against the row's
    ``source_instance`` in ``_handle_delete``.

    Returns None when the URI doesn't match a local row, has no pk
    segment, or the pk is non-numeric. The caller treats None as a
    silent no-op (matches ``_handle_undo``'s "row didn't exist" path).
    """
    if isinstance(target, dict):
        uri = target.get("id")
    elif isinstance(target, str):
        uri = target
    else:
        return None
    if not isinstance(uri, str) or not uri:
        return None

    path = urlparse(uri).path or ""
    # AS2 object URIs from this codebase are minted as
    # ``{origin}/job-posts/{pk}`` (see lib/as_object.object_uri). Pull
    # the pk off the trailing segment; reject anything that doesn't fit
    # rather than guessing — a malformed URI from a misbehaving peer is
    # safer to ignore than to wildcard-match against.
    segments = [seg for seg in path.split("/") if seg]
    if len(segments) < 2 or segments[-2] != "job-posts":
        return None
    raw_pk = segments[-1]
    if not raw_pk.isdigit():
        return None
    return JobPost.objects.filter(pk=int(raw_pk)).first()


def _handle_delete(activity: dict, actor: Actor,
                   verified: federation_signing.VerifiedSignature) -> JsonResponse:
    """Process a `Delete` activity: tombstone the matching federated row.

    Semantics:
    * Resolve the target JobPost from the activity's ``object`` (bare
      URI or Tombstone dict). Unknown URI → silent 202 (no-op log).
    * The sender's instance host (the URI host of the verified actor)
      MUST match the row's ``source_instance``. No cross-instance
      delete authority — a peer can only retract what its own instance
      originated. Mismatch → silent 202 (no-op log) so the audit row
      records the rejected attempt without surfacing internal state.
    * Idempotent: if ``source_deleted_at`` is already populated, leave
      the original tombstone time alone. The audit row still gets
      written (replay protection by ``activity_id`` is upstream of
      this handler), but the column is preserved.
    * Never deletes the row. Local relationships (Score, JobApplication,
      CoverLetter) reference this JobPost; honoring remote delete
      authority by cascading those out would destroy unrelated user
      data.

    The 202 status mirrors every other inbox handler: the verified
    activity was accepted; internal disposition (applied / skipped /
    rejected) lives on the FederationActivity audit row, not on the
    response.
    """
    target = activity.get("object")
    audit_target = target.get("id") if isinstance(target, dict) else (
        target if isinstance(target, str) else None
    )
    _log_inbound(activity, actor, verified, ACTIVITY_TYPE_DELETE, audit_target)

    job_post = _resolve_jobpost_from_object(target)
    if job_post is None:
        return JsonResponse(
            {"status": "accepted"}, status=202, content_type=AS2_CONTENT_TYPE
        )

    # Cross-instance authority guard: only the origin instance can
    # retract its own row. The verified actor's host is the trust
    # anchor (signature verification proved that identity); the
    # row's ``source_instance`` is the trust target.
    sender_host = urlparse(verified.actor_uri).netloc.lower()
    if not sender_host or sender_host != (job_post.source_instance or "").lower():
        return JsonResponse(
            {"status": "accepted"}, status=202, content_type=AS2_CONTENT_TYPE
        )

    # Idempotency — preserve the first-known tombstone time so the
    # audit story doesn't get rewritten by a replay or a re-delivery
    # weeks later.
    if job_post.source_deleted_at is None:
        JobPost.objects.filter(pk=job_post.pk).update(
            source_deleted_at=timezone.now()
        )

    return JsonResponse(
        {"status": "accepted"}, status=202, content_type=AS2_CONTENT_TYPE
    )


def _log_inbound(activity: dict, actor: Actor,
                 verified: federation_signing.VerifiedSignature,
                 activity_type: str, target_uri: str | None) -> FederationActivity | None:
    """Idempotent inbound log writer.

    Idempotency comes from the unique constraint on
    ``(direction, activity_id)``. A racing duplicate POST loses the
    IntegrityError race and returns None; caller treats that as
    "already logged" → silent dedupe.
    """
    activity_id = activity.get("id", "")
    try:
        return FederationActivity.objects.create(
            direction=DIRECTION_INBOUND,
            activity_type=activity_type,
            activity_id=activity_id,
            actor_uri=verified.actor_uri,
            target_uri=target_uri,
            local_user_id=actor.user_id,
            body=json.dumps(activity),
            signature_payload=verified.signature_header,
            received_at=timezone.now(),
            delivery_status=DELIVERY_ACCEPTED,
        )
    except IntegrityError:
        logger.info(
            "ap.inbox.duplicate_activity_id activity_id=%s actor=%s",
            activity_id, verified.actor_uri,
        )
        return None


@csrf_exempt
@require_http_methods(["POST"])
def actor_inbox(request, username: str):
    """Authenticated AP inbox — accept signed POSTs from federation peers."""
    actor = Actor.objects.filter(preferred_username=username).first()
    if actor is None:
        return _inbox_error("actor_not_found", 404)
    # Ensure the local actor has a keypair before any Accept dispatch.
    actor = _ensure_keypair(actor)

    max_bytes = getattr(settings, "ACTIVITYPUB_BODY_MAX_BYTES", 1_048_576)
    body = request.body
    if len(body) > max_bytes:
        return _inbox_error("body_too_large", 400)

    try:
        activity = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _inbox_error("malformed_json", 400)
    if not isinstance(activity, dict):
        return _inbox_error("activity_not_object", 400)

    # Signature verification before rate limit so we know the host bucket
    # we're charging is genuinely the actor's host. Cheap pre-checks
    # (Date / Digest / required headers) live inside
    # ``verify_inbound_signature`` and short-circuit before the network
    # fetch + RSA verify.
    try:
        verified = federation_signing.verify_inbound_signature(request, body)
    except federation_signing.SignatureVerificationError as exc:
        return _inbox_error(exc.verdict, 401)

    # Per-instance rate limit, keyed by the verified actor's host.
    host = urlparse(verified.actor_uri).netloc.lower()
    if not _rate_limit_check(host):
        return _inbox_error("rate_limited", 429)

    # Replay dedupe via activity_id — silent 202 on duplicate.
    activity_id = activity.get("id") or ""
    if activity_id and FederationActivity.objects.filter(
        direction=DIRECTION_INBOUND, activity_id=activity_id,
    ).exists():
        return JsonResponse(
            {"status": "duplicate"},
            status=202,
            content_type=AS2_CONTENT_TYPE,
        )

    activity_type = activity.get("type")
    if activity_type == "Follow":
        return _handle_follow(activity, actor, verified)
    if activity_type == "Undo":
        return _handle_undo(activity, actor, verified)
    if activity_type == "Create":
        return _handle_create(activity, actor, verified)
    if activity_type == "Delete":
        return _handle_delete(activity, actor, verified)

    # Forward-compat: log unknown types as Other so we don't lose them.
    _log_inbound(activity, actor, verified, ACTIVITY_TYPE_OTHER, None)
    return JsonResponse(
        {"status": "accepted"}, status=202, content_type=AS2_CONTENT_TYPE
    )
