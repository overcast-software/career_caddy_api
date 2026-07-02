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
import re
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

from job_hunting.lib.as_object import (
    _actor_rich_capable,
    build_create_activity_for_jobpost,
    build_note_object_for_jobpost,
    resolve_personal_annotations_batch,
)
from job_hunting.lib import federation_signing
from job_hunting.lib import federation_ingest
from job_hunting.lib import federation_inbox
from job_hunting.models import (
    Actor,
    Company,
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
    ACTIVITY_TYPE_UPDATE,
    DELIVERY_ACCEPTED,
    DELIVERY_FAILED,
    DIRECTION_INBOUND,
    DIRECTION_OUTBOUND,
)
from job_hunting.models.actor import ACTOR_TYPE_ORGANIZATION, ACTOR_TYPE_PERSON
from job_hunting.models.job_post import AS2_PUBLIC
from job_hunting.models.nanoid_pk import NANOID_RE


logger = logging.getLogger(__name__)


# Accept-header content type values that signal an AS2 / ActivityPub
# client (Mastodon, Pleroma, peer Career Caddy instance). Anything
# else falls through to the JSON:API / browser branch — drf-json-api
# clients send ``application/vnd.api+json``; the SPA itself sends
# either that or ``*/*``.
_AS2_ACCEPT_TYPES = (
    "application/activity+json",
    "application/ld+json",
)


def _wants_activitypub(request) -> bool:
    """Return True when the client asked for AS2 JSON via Accept.

    Strict prefix match — ``application/activity+json`` is the canonical
    spelling, ``application/ld+json; profile="..."`` is what Mastodon
    sometimes sends, both count. Empty / wildcard Accept → False so the
    JSON:API default wins for browsers + drf-json-api clients.
    """
    accept = request.META.get("HTTP_ACCEPT", "") or ""
    if not accept or accept == "*/*":
        return False
    for chunk in accept.split(","):
        media = chunk.split(";", 1)[0].strip().lower()
        if media in _AS2_ACCEPT_TYPES:
            return True
    return False


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


def _company_actor_uri(slug: str) -> str:
    """Mint the Organization-actor URI for a Company.

    Phase 6a — Company actors are surfaced at ``/companies/<slug>/``
    rather than ``/actors/<username>/`` so a Mastodon user pasting
    ``acct:acme@careercaddy.online`` lands on the Company page itself
    (Q1 / Q2 in the Phase 6 plan — federation handle == public Company
    page URL).
    """
    return f"{_origin()}/companies/{slug}"


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

    # Phase 6a — resolve in two steps. Person / Service / Application
    # actors live on ``Actor.preferred_username``; Organization actors
    # for Companies live on ``Company.slug``. Person lookup first so
    # the common case (Mastodon discovering a user account) stays a
    # single index probe; Company fallback only fires on a miss.
    actor = Actor.objects.filter(preferred_username=username).first()
    if actor is not None and actor.company_id is None:
        actor_uri = _actor_uri(actor.preferred_username)
    else:
        company = Company.objects.filter(slug=username).first()
        if company is None:
            return JsonResponse(
                {"error": "not found"}, status=404, content_type=JRD_CONTENT_TYPE
            )
        actor_uri = _company_actor_uri(company.slug)

    jrd = {
        "subject": f"acct:{username}@{_origin_host()}",
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


def public_jobpost_queryset_for_user(user_id):
    """Return a user's public-audience JobPosts in outbox order.

    This is the ONE definition of "federated / published" for a user: a
    post is public iff its ``audience`` contains the AS2 Public URI AND
    it was created by ``user_id``. Posts with empty / private audiences
    are excluded by definition — they were never federable.

    Both surfaces that expose a user's posts publicly consume this so the
    fediverse view and the human-readable view never disagree: the
    ActivityPub actor outbox (:func:`_outbox_jobpost_queryset`) and the
    public web profile endpoint
    (``views.public_profile.public_user_federated_job_posts``).

    When ``user_id is None`` (the future instance-actor case) the
    queryset is empty so callers render zero items rather than 500'ing on
    the absent owner.

    Sort: ``-created_at`` then ``-id`` so identical creation timestamps
    (paste-storms, demo seed) have a stable secondary order — important
    once peers diff pages between requests.
    """
    if user_id is None:
        return JobPost.objects.none()
    return (
        JobPost.objects.filter(
            created_by_id=user_id,
            audience__contains=[AS2_PUBLIC],
        )
        .order_by("-created_at", "-id")
    )


def _outbox_jobpost_queryset(actor: Actor):
    """Return the actor's public-audience JobPosts in outbox order.

    Thin adapter over :func:`public_jobpost_queryset_for_user` keyed off
    the actor's owning user — the outbox and the public web profile share
    one filter definition so "published" means the same thing on both.
    """
    return public_jobpost_queryset_for_user(actor.user_id)


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
    # select_related the owner FK + company so the per-Note builder never
    # lazy-loads them per row (BACK-100): the page is rendered with a
    # bounded query count regardless of size.
    queryset = _outbox_jobpost_queryset(actor).select_related("company")
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
    page_posts = list(queryset[offset : offset + page_size])
    # BACK-100: resolve the owner's verdict/score/applied for the whole
    # page in a fixed number of queries (instead of N+1 per Note), then
    # feed the prefetched annotations into each builder so a peer crawling
    # the outbox sees the SAME rich body the delivered Create/Update
    # carried. Lean actors (not rich-opted-in) skip the resolve entirely.
    rich = _actor_rich_capable(actor)
    annotation_map = (
        resolve_personal_annotations_batch(page_posts, actor.user_id)
        if rich
        else {}
    )
    items = [
        build_create_activity_for_jobpost(
            job_post,
            actor,
            rich=rich,
            annotations=annotation_map.get(job_post.pk),
        )
        for job_post in page_posts
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
# BACK-93 — AP object dereferencing (Note objects + activity envelopes).
#
# Every outbox / delivered ``Create`` advertises
# ``object.id = {origin}/job-posts/<pk>`` and
# ``id = {origin}/activities/<uuid>``. Remote instances (Mastodon et al.)
# dereference those URIs to verify + render the post. Without these views
# the apex Caddy falls through to the SPA and the peer gets HTML it can't
# ingest — so federated posts render nowhere on the remote profile. Both
# views serve AS2; the ``/job-posts/<pk>`` surface content-negotiates so
# the human SPA path is undisturbed (only an AP ``Accept`` reaches the
# Note branch).


def _jobpost_is_local(job_post: JobPost) -> bool:
    """True when this instance is authoritative for the JobPost's object id.

    Federated rows carry a ``source_instance`` and their canonical object
    id is rooted on the origin instance (see ``as_object.object_uri``);
    we must not claim authority for them under our own origin. Local rows
    (``source_instance`` unset or equal to our host) are ours to serve.
    """
    src = job_post.source_instance
    return not src or src == settings.CAREER_CADDY_INSTANCE


def _jobpost_is_public(job_post: JobPost) -> bool:
    """True when the post is federated-public (audience holds AS2 Public)."""
    audience = job_post.audience
    return isinstance(audience, list) and AS2_PUBLIC in audience


@csrf_exempt
@require_http_methods(["GET"])
def jobpost_object_view(request, pk: str):
    """Content-negotiated JobPost object — AS2 Note for federation peers.

    AS2 branch (``Accept: application/activity+json`` / ``ld+json``):
    serve the standalone Note iff the post is local AND public (its
    ``audience`` contains the AS2 Public URI). Private / non-local /
    missing posts → 404 AS2 — a private post must never become
    dereferenceable just because the caller sent an AP ``Accept`` header
    (preserves the BACK-91 private-by-default + outbox visibility math).

    Default branch: minimal JSON:API ``job-post`` linkage (mirrors
    ``company_actor_view``). Under the Accept-gated apex routing rule a
    browser never reaches here — this branch is defensive only.
    """
    job_post = JobPost.objects.filter(pk=pk).first()
    public = (
        job_post is not None
        and _jobpost_is_public(job_post)
        and _jobpost_is_local(job_post)
    )

    if _wants_activitypub(request):
        if not public:
            return JsonResponse(
                {"error": "not found"}, status=404, content_type=AS2_CONTENT_TYPE
            )
        actor = (
            Actor.objects.filter(
                user_id=job_post.created_by_id, type=ACTOR_TYPE_PERSON
            )
            .order_by("pk")
            .first()
        )
        body = build_note_object_for_jobpost(job_post, actor)
        return HttpResponse(
            content=JsonResponse(body).content,
            content_type=AS2_CONTENT_TYPE,
        )

    if not public:
        return JsonResponse(
            {"error": "not found"}, status=404, content_type=AS2_CONTENT_TYPE
        )
    body = {
        "data": {
            "type": "job-post",
            "id": str(job_post.pk),
            "links": {
                "self": f"{_origin()}/api/v1/job-posts/{job_post.pk}/",
            },
        }
    }
    return HttpResponse(
        content=JsonResponse(body).content,
        content_type="application/vnd.api+json",
    )


@csrf_exempt
@require_http_methods(["GET"])
def federation_activity_view(request, activity_uuid: str):
    """Dereference an outbound activity envelope by its UUID.

    Every delivered ``Create`` / ``Update`` / ``Delete`` persists a
    ``FederationActivity`` outbound row whose ``activity_id`` is the full
    ``{origin}/activities/<uuid>`` URI and whose ``body`` is the JSON we
    signed + sent. We resolve the row by that URI and replay the stored
    body as AS2 — so a peer dereferencing an activity id gets the
    activity, not the SPA HTML. Unknown / inbound-only ids → 404 AS2.

    Only OUTBOUND rows are served: inbound activities belong to the peer
    that authored them, not to us.
    """
    activity_id = f"{_origin()}/activities/{activity_uuid}"
    row = (
        FederationActivity.objects.filter(
            direction=DIRECTION_OUTBOUND, activity_id=activity_id
        )
        .order_by("pk")
        .first()
    )
    if row is None or not row.body:
        return JsonResponse(
            {"error": "not found"}, status=404, content_type=AS2_CONTENT_TYPE
        )
    try:
        body = json.loads(row.body)
    except (TypeError, ValueError):
        return JsonResponse(
            {"error": "not found"}, status=404, content_type=AS2_CONTENT_TYPE
        )
    return HttpResponse(
        content=JsonResponse(body).content,
        content_type=AS2_CONTENT_TYPE,
    )


# ---------------------------------------------------------------------------
# Phase 6a — Company / Organization actors.
#
# A Company-actor row owns the AS2 ``Organization`` surface for a
# Company: WebFinger resolves ``acct:<slug>@<host>`` to the row, the
# Actor URI sits at ``/companies/<slug>/``, and the outbox lists the
# Company's federation-enabled JobPosts as Create(Note) activities.
#
# Two key decisions vs Phase 5a Person actors:
#
# 1. URL shape. Person actors live at ``/actors/<username>/``; Company
#    actors live at ``/companies/<slug>/`` so a Mastodon user who
#    follows the actor and clicks through lands on the public Company
#    page directly. The two URI families share zero routes.
#
# 2. Lazy materialization. Phase 5a backfilled one Actor row per User
#    via ``generate_federation_actors``. Companies are too numerous to
#    pre-create eagerly (thousands of scraped rows in prod). We
#    instead create the Actor row on first AS2 hit — the
#    ``_ensure_company_actor`` helper below upserts under SELECT FOR
#    UPDATE so the lazy-keypair contract from 5a still holds.


def _ensure_company_actor(company: Company) -> Actor:
    """Return or create the Organization Actor for ``company``.

    Idempotent + race-safe. The ``preferred_username == company.slug``
    invariant lets WebFinger reuse its single index probe to find both
    Person and Organization actors. SELECT FOR UPDATE serialises the
    rare case where two AS2 fetches race to create the same row; the
    second waiter sees the first one's row after acquiring the lock.

    Caller must ensure ``company.slug`` is populated — Companies whose
    backfill hasn't run yet shouldn't reach this code path; the views
    above gate on slug presence and 404 if it's missing.
    """
    existing = Actor.objects.filter(company_id=company.id).first()
    if existing is not None:
        return existing
    with transaction.atomic():
        # Race-safe upsert: re-check inside the lock, then create.
        existing = (
            Actor.objects.select_for_update()
            .filter(company_id=company.id)
            .first()
        )
        if existing is not None:
            return existing
        return Actor.objects.create(
            company=company,
            type=ACTOR_TYPE_ORGANIZATION,
            preferred_username=company.slug,
        )


def _company_actor_body(company: Company, actor: Actor) -> dict:
    """Build the AS2 Organization JSON-LD body for ``company`` / ``actor``."""
    actor_uri = _company_actor_uri(company.slug)
    body = {
        "@context": [
            "https://www.w3.org/ns/activitystreams",
            "https://w3id.org/security/v1",
        ],
        "id": actor_uri,
        "type": actor.type,
        "preferredUsername": company.slug,
        "url": actor_uri,
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
    display = company.display_name or company.name
    if display:
        body["name"] = display
    if actor.avatar_url:
        # AS2 ``icon`` is the canonical key for a Person/Organization
        # avatar/logo. Mastodon's UI surfaces it as the actor's badge.
        body["icon"] = {"type": "Image", "url": actor.avatar_url}
    return body


@csrf_exempt
@require_http_methods(["GET"])
def company_actor_view(request, slug: str):
    """Content-negotiated Company page — AS2 Organization for federation
    peers, JSON:API Company resource for SPA/browser clients.

    AS2 branch (``Accept: application/activity+json`` /
    ``application/ld+json``): mints the lazy Organization Actor row +
    keypair, returns the AS2 JSON-LD body.

    Default branch: 302 to the SPA frontend's Company detail page
    (``/companies/<id>``). drf-json-api clients hit the existing
    ``/api/v1/companies/<pk>/`` viewset directly; this view is the
    public ``/companies/<slug>/`` surface aimed at humans + AP peers,
    not the SPA's own data API.
    """
    company = Company.objects.filter(slug=slug).first()
    if company is None:
        return JsonResponse(
            {"error": "not found"}, status=404, content_type=AS2_CONTENT_TYPE
        )

    if _wants_activitypub(request):
        actor = _ensure_company_actor(company)
        actor = _ensure_keypair(actor)
        body = _company_actor_body(company, actor)
        return HttpResponse(
            content=JsonResponse(body).content,
            content_type=AS2_CONTENT_TYPE,
        )

    # Browser / default branch — JSON:API doesn't have a clean shape
    # for "this is the slug-routed sibling of the pk-routed resource";
    # we return a minimal JSON-API-shaped Company linkage so the SPA
    # (or curl-from-shell) sees something useful, AND set the canonical
    # ``Link`` header to the pk-routed CRUD endpoint for downstream
    # tooling. The frontend will likely move to a direct
    # ``/companies/<slug>`` route in a separate dispatch.
    body = {
        "data": {
            "type": "company",
            "id": str(company.id),
            "attributes": {
                "name": company.name,
                "display-name": company.display_name,
                "slug": company.slug,
                "federation-enabled": company.federation_enabled,
            },
            "links": {
                "self": f"{_origin()}/api/v1/companies/{company.id}/",
            },
        }
    }
    response = HttpResponse(
        content=JsonResponse(body).content,
        content_type="application/vnd.api+json",
    )
    return response


def _company_outbox_queryset(company: Company):
    """Public JobPosts attributed to ``company`` in outbox order.

    Filter: ``audience`` contains AS2_PUBLIC AND ``company_id``
    matches. Sort: ``-created_at, -id`` mirrors the Person-actor
    outbox so per-Company federation pages have the same stable
    secondary ordering once paging starts mattering.
    """
    return (
        JobPost.objects.filter(
            company_id=company.id,
            audience__contains=[AS2_PUBLIC],
        )
        .order_by("-created_at", "-id")
    )


def _build_company_create_activity(job_post: JobPost, company: Company, actor: Actor) -> dict:
    """Build the Create(Note) envelope attributed to a Company actor.

    Parallel to ``build_create_activity_for_jobpost`` (used by Person
    actors) but rewrites ``actor`` + ``attributedTo`` to point at the
    Company actor's URI. Reusing the Phase 5b helper directly would
    bake in the Person-actor URI shape via ``actor.preferred_username``;
    we want the ``/companies/<slug>/`` URI shape on the activity here.
    """
    activity = build_create_activity_for_jobpost(job_post, actor)
    company_actor_uri = _company_actor_uri(company.slug)
    activity["actor"] = company_actor_uri
    activity["cc"] = [f"{company_actor_uri}/followers"]
    inner = activity.get("object")
    if isinstance(inner, dict):
        inner["attributedTo"] = company_actor_uri
    return activity


@csrf_exempt
@require_http_methods(["GET"])
def company_outbox(request, slug: str):
    """Paginated OrderedCollection of the Company's public JP Create activities.

    Mirrors the Phase 5b Person-actor outbox: metadata-only collection
    on no ``page``; ``?page=N`` returns an OrderedCollectionPage with
    up to ``ACTIVITYPUB_OUTBOX_PAGE_SIZE`` Create(Note) envelopes.
    Items are built fresh per request — Phase 6a does not persist
    outbox-specific Activity rows (the 5d dispatch path does, and
    those rows aren't shown in the outbox enumeration).
    """
    company = Company.objects.filter(slug=slug).first()
    if company is None:
        return JsonResponse(
            {"error": "not found"}, status=404, content_type=AS2_CONTENT_TYPE
        )
    actor = _ensure_company_actor(company)

    collection_id = f"{_company_actor_uri(company.slug)}/outbox"
    page_size = getattr(settings, "ACTIVITYPUB_OUTBOX_PAGE_SIZE", 20)
    queryset = _company_outbox_queryset(company)
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
        _build_company_create_activity(job_post, company, actor)
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


_KEY_ID_RE = re.compile(r'keyId="([^"]+)"')


def _claimed_signature_host(sig_header: str) -> str:
    """Best-effort netloc of the ``keyId`` in a Signature header (UNVERIFIED).

    CC-127: signature verification is now async, so the edge throttles on
    the *claimed* keyId host rather than a verified one. Spoofable, but it
    correctly buckets legit peers (claimed == verified host) and bounds a
    single naive / misbehaving peer's queue pressure. Returns ``""`` when
    unparseable → ``_rate_limit_check`` lets it through and the async
    verify is the real gate.
    """
    if not sig_header:
        return ""
    m = _KEY_ID_RE.search(sig_header)
    if not m:
        return ""
    return urlparse(m.group(1).split("#", 1)[0]).netloc.lower()


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
    # JobPost PKs are 10-char NanoIDs (CC-57), no longer integers. Reject
    # anything that isn't a well-formed NanoID rather than guessing, and
    # look up by the string pk directly (no int() cast).
    if not NANOID_RE.match(raw_pk):
        return None
    return JobPost.objects.filter(pk=raw_pk).first()


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
    # weeks later. Phase 6b extends the tombstone with explicit
    # ``posting_status=closed`` + ``complete=False`` flips so the
    # closed-row visibility rules + thin-stub gates respond to the
    # remote retraction without the operator having to flip them by
    # hand. Only fire on the first known tombstone — subsequent
    # replays preserve whatever state the user / staff have since
    # adjusted.
    if job_post.source_deleted_at is None:
        JobPost.objects.filter(pk=job_post.pk).update(
            source_deleted_at=timezone.now(),
            posting_status="closed",
            complete=False,
        )

    return JsonResponse(
        {"status": "accepted"}, status=202, content_type=AS2_CONTENT_TYPE
    )


def _handle_update(activity: dict, actor: Actor,
                   verified: federation_signing.VerifiedSignature) -> JsonResponse:
    """Process an inbound ``Update(Note)`` — merge empty fields only.

    Phase 6b — when the origin instance updates a JobPost we previously
    ingested via federation, merge new values into local fields that
    are currently empty. Never clobber local non-empty values (a staff
    edit must outrank stale upstream data). Cross-instance authority
    guard mirrors ``_handle_delete``: only the source instance can
    update its own row.

    Returns 202 in every outcome (matches every other inbox handler);
    the audit row's ``delivery_status`` carries the merge / reject
    decision for operator visibility.
    """
    inner = activity.get("object")
    target_uri = inner.get("id") if isinstance(inner, dict) else None
    audit_row = _log_inbound(activity, actor, verified, ACTIVITY_TYPE_UPDATE, target_uri)

    if getattr(settings, "ACTIVITYPUB_INGEST_ENABLED", True) and audit_row is not None:
        federation_ingest.ingest_update_note(activity, federation_activity=audit_row)

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


def dispatch_verified_person_activity(activity: dict, actor: Actor,
                                      verified: federation_signing.VerifiedSignature) -> None:
    """Route a VERIFIED inbound activity to its Person-actor handler.

    CC-127: runs in the qcluster worker off the web thread. The
    ``_handle_*`` return values (JsonResponse) are meaningful only to the
    old synchronous edge; here we keep the side effects + audit rows and
    discard the response. Replay dedupe is done here (post-verify) since
    the edge no longer has a ``verified`` identity to key on.
    """
    activity_id = activity.get("id") or ""
    if activity_id and FederationActivity.objects.filter(
        direction=DIRECTION_INBOUND, activity_id=activity_id,
    ).exists():
        logger.info("ap.inbox.duplicate_activity_id activity_id=%s", activity_id)
        return

    activity_type = activity.get("type")
    if activity_type == "Follow":
        _handle_follow(activity, actor, verified)
    elif activity_type == "Undo":
        _handle_undo(activity, actor, verified)
    elif activity_type == "Create":
        _handle_create(activity, actor, verified)
    elif activity_type == "Update":
        _handle_update(activity, actor, verified)
    elif activity_type == "Delete":
        _handle_delete(activity, actor, verified)
    else:
        # Forward-compat: log unknown types as Other so we don't lose them.
        _log_inbound(activity, actor, verified, ACTIVITY_TYPE_OTHER, None)


@csrf_exempt
@require_http_methods(["POST"])
def actor_inbox(request, username: str):
    """Authenticated AP inbox — accept-then-async (CC-127).

    The edge does only cheap, network-free work and returns 202
    immediately; the expensive HTTP-Signature verify (remote key fetch +
    RSA) + activity processing run on the django-q worker via
    ``federation_inbox.enqueue_inbound_activity``. This never blocks a web
    worker on a slow / dead peer's key endpoint — the CC-127 defect.

    Edge order (all network-free):
      1. actor exists                          → 404
      2. body size cap                         → 400
      3. JSON parse gate                       → 400
      4. unknown-actor self-Delete/Update drop → 202 (Mastodon
         skip_unknown_actor_activity; no fetch, no enqueue)
      5. cheap signature precheck              → 401 (tampered/unsigned/
         stale/bad-digest — rejected without a queue slot)
      6. per-host throttle (claimed keyId host)→ 429
      7. enqueue verify+process                → 202
    """
    if not Actor.objects.filter(preferred_username=username).exists():
        return _inbox_error("actor_not_found", 404)

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

    # Mastodon skip_unknown_actor_activity: self-Delete/Update from an
    # actor we've never seen is inherently unverifiable (deleted account,
    # key endpoint gone) — accept + drop before any fetch or enqueue.
    if federation_inbox.is_droppable_unknown_actor_activity(activity):
        return JsonResponse(
            {"status": "accepted"}, status=202, content_type=AS2_CONTENT_TYPE
        )

    # Cheap, NETWORK-FREE signature precheck (required headers, Date
    # window, Digest). Reject here without a queue slot; the expensive
    # remote key fetch + RSA verify is deferred to the worker.
    try:
        federation_signing.verify_inbound_signature_precheck(request, body)
    except federation_signing.SignatureVerificationError as exc:
        return _inbox_error(exc.verdict, 401)

    # Coarse per-instance throttle on the CLAIMED keyId host.
    claimed_host = _claimed_signature_host(request.headers.get("Signature", ""))
    if not _rate_limit_check(claimed_host):
        return _inbox_error("rate_limited", 429)

    federation_inbox.enqueue_inbound_activity(
        actor_kind="person",
        identifier=username,
        method=request.method,
        path=request.path,
        headers=dict(request.headers),
        body=body,
    )
    return JsonResponse(
        {"status": "accepted"}, status=202, content_type=AS2_CONTENT_TYPE
    )


# ---------------------------------------------------------------------------
# Phase 6b — Company-actor inbox + Follow handshake.
#
# Mirrors the Phase 5c Person-actor inbox shape (pre-flight order, audit
# log, replay protection, rate limit) but keys follower rows off the
# Company FK so the Phase 6b ingest helper (which resolves followers by
# ``company_id``) can materialize discoveries on inbound Create(Note)s
# attributed to a local Company actor.
#
# The Person-actor handlers (``_handle_follow``, ``_handle_undo``,
# ``_deliver_accept``) are kept intact; the Company-actor flow gets its
# own handlers below so the Phase 5c test surface stays unchanged.
#
# Acceptance contract:
# - Follow → Accept dispatched to the remote actor's inbox, follower row
#   keyed off (company, actor_uri) persisted, listed on
#   ``/companies/<slug>/followers``.
# - Undo(Follow) → unfollowed_at set on the matching row.
# - Other / Create / Update / Delete → forwarded to the existing Phase
#   5c/6b handlers; Create + Update + Delete reuse the Person-actor
#   dispatch because their resolution keys off canonical_link /
#   source_instance, not the inbox the activity arrived on.
# - Activity-id replay dedupe is shared with the Person-actor inbox via
#   the same FederationActivity unique constraint; a peer can't replay a
#   Follow against both inboxes to bypass it.


def _log_company_inbound(
    activity: dict,
    company: Company,
    verified: federation_signing.VerifiedSignature,
    activity_type: str,
    target_uri: str | None,
) -> FederationActivity | None:
    """Idempotent inbound log writer for Company-actor inboxes.

    Mirrors :func:`_log_inbound` but leaves ``local_user`` NULL —
    Company-actor activities don't tie to a single User. The
    ``(direction, activity_id, target_uri)`` unique constraint still
    short-circuits duplicate POSTs the same way.
    """
    activity_id = activity.get("id", "")
    try:
        return FederationActivity.objects.create(
            direction=DIRECTION_INBOUND,
            activity_type=activity_type,
            activity_id=activity_id,
            actor_uri=verified.actor_uri,
            target_uri=target_uri,
            local_user_id=None,
            body=json.dumps(activity),
            signature_payload=verified.signature_header,
            received_at=timezone.now(),
            delivery_status=DELIVERY_ACCEPTED,
        )
    except IntegrityError:
        logger.info(
            "ap.company_inbox.duplicate_activity_id activity_id=%s actor=%s",
            activity_id, verified.actor_uri,
        )
        return None


def _deliver_company_accept(
    follow_activity: dict,
    follower: FederationFollower,
    company: Company,
    actor: Actor,
) -> FederationActivity:
    """Build, sign, and POST Accept(Follow) from a Company actor.

    Mirrors :func:`_deliver_accept` but signs with the Company-actor's
    ``/companies/<slug>`` URI (passed through ``federation_signing.deliver``
    via the ``actor_uri`` override) so the peer's signature verifier
    fetches the Company actor's public key, not a Person-actor key.
    """
    actor_uri_str = _company_actor_uri(company.slug)
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
        local_user_id=None,
        body=json.dumps(accept_body),
        delivery_status="pending",
    )

    status_code, snippet = federation_signing.deliver(
        follower.inbox_uri, body_bytes, actor, actor_uri=actor_uri_str,
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


def _handle_company_follow(
    activity: dict,
    company: Company,
    actor: Actor,
    verified: federation_signing.VerifiedSignature,
) -> JsonResponse:
    """Process a Follow targeting a local Company actor."""
    actor_uri_local = _company_actor_uri(company.slug)
    target = activity.get("object")
    if target != actor_uri_local:
        return _inbox_error("follow_target_mismatch", 422)

    follower_actor_uri = activity.get("actor") or verified.actor_uri
    if not follower_actor_uri:
        return _inbox_error("missing_actor", 422)

    inbox_uri, shared_inbox_uri = _peer_actor_endpoints(follower_actor_uri)
    host = FederationFollower.host_for_uri(follower_actor_uri)

    follower, _ = FederationFollower.objects.update_or_create(
        company_id=company.id,
        actor_uri=follower_actor_uri,
        defaults={
            "inbox_uri": inbox_uri,
            "shared_inbox_uri": shared_inbox_uri,
            "instance_host": host,
            "unfollowed_at": None,  # re-follow case: clear any prior Undo
            "local_user": None,
        },
    )

    _log_company_inbound(activity, company, verified, ACTIVITY_TYPE_FOLLOW, target)

    _deliver_company_accept(activity, follower, company, actor)

    return JsonResponse(
        {"status": "accepted"}, status=202, content_type=AS2_CONTENT_TYPE
    )


def _handle_company_undo(
    activity: dict,
    company: Company,
    actor: Actor,
    verified: federation_signing.VerifiedSignature,
) -> JsonResponse:
    """Process an Undo(Follow) targeting a local Company actor."""
    inner = activity.get("object") or {}
    if not isinstance(inner, dict):
        return _inbox_error("undo_object_not_object", 422)
    if inner.get("type") != "Follow":
        # Forward-compat: forward any non-Follow Undo to the Other bucket
        # so the audit row carries the activity without 422'ing the peer.
        _log_company_inbound(activity, company, verified, ACTIVITY_TYPE_OTHER, None)
        return JsonResponse(
            {"status": "accepted"}, status=202, content_type=AS2_CONTENT_TYPE
        )

    actor_uri_local = _company_actor_uri(company.slug)
    if inner.get("object") != actor_uri_local:
        return _inbox_error("undo_target_mismatch", 422)

    follower_actor_uri = inner.get("actor") or verified.actor_uri
    row = FederationFollower.objects.filter(
        company_id=company.id,
        actor_uri=follower_actor_uri,
    ).first()
    if row is None:
        _log_company_inbound(
            activity, company, verified, ACTIVITY_TYPE_UNDO, actor_uri_local
        )
        return _inbox_error("not_following", 404)

    FederationFollower.objects.filter(pk=row.pk).update(unfollowed_at=timezone.now())
    _log_company_inbound(
        activity, company, verified, ACTIVITY_TYPE_UNDO, actor_uri_local
    )

    return JsonResponse(
        {"status": "accepted"}, status=202, content_type=AS2_CONTENT_TYPE
    )


def _handle_company_create(
    activity: dict,
    company: Company,
    actor: Actor,
    verified: federation_signing.VerifiedSignature,
) -> JsonResponse:
    """Process a Create activity arriving on a Company-actor inbox.

    Mirrors the Person-actor :func:`_handle_create` semantics — log
    the activity, then hand off to the Phase 6b ingest helper which
    resolves discoveries via ``attributedTo``. Audit row carries the
    Company-actor target URI for traceability.
    """
    inner = activity.get("object") or {}
    target = inner.get("id") if isinstance(inner, dict) else None
    audit_row = _log_company_inbound(
        activity, company, verified, ACTIVITY_TYPE_CREATE, target
    )

    if getattr(settings, "ACTIVITYPUB_INGEST_ENABLED", True):
        if audit_row is not None:
            federation_ingest.ingest_create_note(
                activity, federation_activity=audit_row
            )

    return JsonResponse(
        {"status": "accepted"}, status=202, content_type=AS2_CONTENT_TYPE
    )


def dispatch_verified_company_activity(activity: dict, company: Company, actor: Actor,
                                       verified: federation_signing.VerifiedSignature) -> None:
    """Route a VERIFIED inbound activity to its Company-actor handler.

    CC-127 worker-side counterpart to :func:`dispatch_verified_person_activity`.
    Update / Delete reuse the Person-actor handlers (they resolve targets by
    canonical_link / source_instance, not by the inbox they arrived on); the
    synthetic Company actor leaves ``local_user`` NULL on the audit row.
    """
    activity_id = activity.get("id") or ""
    if activity_id and FederationActivity.objects.filter(
        direction=DIRECTION_INBOUND, activity_id=activity_id,
    ).exists():
        logger.info("ap.company_inbox.duplicate_activity_id activity_id=%s", activity_id)
        return

    activity_type = activity.get("type")
    if activity_type == "Follow":
        _handle_company_follow(activity, company, actor, verified)
    elif activity_type == "Undo":
        _handle_company_undo(activity, company, actor, verified)
    elif activity_type == "Create":
        _handle_company_create(activity, company, actor, verified)
    elif activity_type == "Update":
        _handle_update(activity, actor, verified)
    elif activity_type == "Delete":
        _handle_delete(activity, actor, verified)
    else:
        _log_company_inbound(activity, company, verified, ACTIVITY_TYPE_OTHER, None)


@csrf_exempt
@require_http_methods(["POST"])
def company_actor_inbox(request, slug: str):
    """Authenticated AP inbox for a Company actor — accept-then-async (CC-127).

    Same thin edge as :func:`actor_inbox`: cheap network-free checks +
    202, with verify+process deferred to the qcluster worker. The Company
    actor row is lazily created + keypair-minted in the WORKER on first
    hit (moved off the edge) so a peer can Follow a Company that hasn't
    received any AS2 traffic yet without blocking the web thread.
    """
    if not Company.objects.filter(slug=slug).exists():
        return _inbox_error("company_not_found", 404)

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

    if federation_inbox.is_droppable_unknown_actor_activity(activity):
        return JsonResponse(
            {"status": "accepted"}, status=202, content_type=AS2_CONTENT_TYPE
        )

    try:
        federation_signing.verify_inbound_signature_precheck(request, body)
    except federation_signing.SignatureVerificationError as exc:
        return _inbox_error(exc.verdict, 401)

    claimed_host = _claimed_signature_host(request.headers.get("Signature", ""))
    if not _rate_limit_check(claimed_host):
        return _inbox_error("rate_limited", 429)

    federation_inbox.enqueue_inbound_activity(
        actor_kind="company",
        identifier=slug,
        method=request.method,
        path=request.path,
        headers=dict(request.headers),
        body=body,
    )
    return JsonResponse(
        {"status": "accepted"}, status=202, content_type=AS2_CONTENT_TYPE
    )


def _company_followers_queryset(company: Company):
    """Active ``FederationFollower`` rows targeting ``company``.

    Mirrors :func:`_followers_queryset` but keys off the ``company`` FK
    so the Phase 6b Company-actor followers collection enumerates the
    correct set. Sort: ``-accepted_at, -created_at, -id`` matches the
    Person-actor shape so peer UIs see confirmed followers first.
    """
    return (
        FederationFollower.objects.filter(
            company_id=company.id,
            unfollowed_at__isnull=True,
        )
        .order_by("-accepted_at", "-created_at", "-id")
    )


@csrf_exempt
@require_http_methods(["GET"])
def company_followers(request, slug: str):
    """Paginated ``OrderedCollection`` of a Company actor's followers."""
    company = Company.objects.filter(slug=slug).first()
    if company is None:
        return JsonResponse(
            {"error": "not found"}, status=404, content_type=AS2_CONTENT_TYPE
        )

    collection_id = f"{_company_actor_uri(company.slug)}/followers"
    page_size = getattr(settings, "ACTIVITYPUB_OUTBOX_PAGE_SIZE", 20)
    queryset = _company_followers_queryset(company)
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
def company_following(request, slug: str):
    """Empty ``OrderedCollection`` stub for the Company actor's following list.

    Company actors don't follow remote actors in V1; the empty collection
    keeps peer enumeration succeeding rather than 404'ing.
    """
    company = Company.objects.filter(slug=slug).first()
    if company is None:
        return JsonResponse(
            {"error": "not found"}, status=404, content_type=AS2_CONTENT_TYPE
        )
    collection_id = f"{_company_actor_uri(company.slug)}/following"
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
