"""ActivityPub inbound inbox — accept-then-async worker (CC-127).

The inbox edge (``job_hunting.api.views.federation.actor_inbox`` /
``company_actor_inbox``) used to verify the HTTP Signature synchronously
on the web-request thread — fetching the remote sender's public key over
HTTP inline. A slow / dead / redirect-looping peer pinned a gunicorn
worker for up to ~40s, and ~45% of deliveries failed verification (401)
— the failures being the *slowest* requests. At ~2200 inbound POSTs/day
this saturated the web-worker pool and starved the UI.

Fix: the edge does only cheap, network-free work (body cap, JSON gate,
the Mastodon ``skip_unknown_actor_activity`` pre-drop, a network-free
signature *precheck*, a per-host throttle) and returns **202
immediately**, enqueuing verify+process to the django-q2 qcluster. This
mirrors the OUTBOUND dispatch worker (``federation_dispatch.dispatch_one``
scheduled by ``enqueue_jobpost_activity``) — the expensive network I/O
never touches a web thread.

Two entry points, same shape as the outbound module:

1. ``enqueue_inbound_activity(...)`` — synchronous, called by the edge.
   Schedules ``process_inbound_activity`` via ``async_task`` (or runs it
   in-band when ``ACTIVITYPUB_INBOX_DISPATCH_SYNC`` is set — the test /
   operator knob).

2. ``process_inbound_activity(...)`` — qcluster worker entry point.
   Re-runs the FULL signature verify (cheap checks + bounded, negatively
   cached remote key fetch + RSA) as the real trust gate, then dispatches
   by activity type via the existing Person / Company handlers.

Design notes vs. Mastodon (validated against its reference impl):
- Mastodon keeps signature verify synchronous but cheap (cached keys +
  circuit breaker) and defers only *processing*. We defer the whole
  verify to the worker (Akkoma "Optimistic Inbox" shape) because the
  RCA's hard requirement is "never block a web worker on remote key
  fetch" — Mastodon's cold-key fetch still runs on its web thread.
- We adopt Mastodon's highest-leverage mitigation verbatim:
  ``is_droppable_unknown_actor_activity`` == its ``skip_unknown_actor_activity``
  before-action, dropping self-Delete/Update from unknown actors before
  any fetch (the dominant unverifiable-traffic slice).
- Accept-then-async trades away Mastodon's 401-retry safety net: a peer
  whose key is *transiently* unfetchable is 202'd then dropped (won't
  redeliver). The 10s worker read-timeout catches most slow-but-alive
  peers on the first attempt; worker-internal retry / instance-actor-
  signed Deletes are documented fast-follows.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from django.conf import settings

from job_hunting.lib import federation_signing
from job_hunting.models import (
    Actor,
    Company,
    FederationActivity,
    FederationFollower,
)
from job_hunting.models.federation_activity import DIRECTION_INBOUND


log = logging.getLogger(__name__)


# django-q task path — kept as a literal so the qcluster imports the same
# dotted path the enqueue site schedules.
_TASK_PATH = "job_hunting.lib.federation_inbox.process_inbound_activity"


@dataclass
class _ReplayRequest:
    """Minimal stand-in for the Django request the worker re-verifies.

    ``federation_signing.verify_inbound_signature`` only touches
    ``.method``, ``.path`` and ``.headers`` (a mapping with ``.items()``).
    The edge captures those off the live request and passes them through
    the queue so the worker can rebuild the exact signed-string inputs.
    """

    method: str
    path: str
    headers: dict = field(default_factory=dict)


def is_droppable_unknown_actor_activity(activity: dict) -> bool:
    """Mastodon ``skip_unknown_actor_activity`` equivalent (CC-127).

    A self-referential ``Delete``/``Update`` (``actor == object.id``) from
    an actor we have never interacted with is inherently unverifiable: a
    deleted fediverse account broadcasts a signed ``Delete`` of itself to
    every peer, then its key endpoint 404/410s, so the signature can never
    be fetched. Mastodon drops these BEFORE signature verification (``head
    202``) to keep them off the key-fetch path — the single highest-
    leverage defense against the traffic behind our 401 pile-up.

    Safe in our schema: a self-``Delete``'s object is an actor URI, not a
    ``/job-posts/<pk>`` URI, so ``_handle_delete`` already no-ops on it —
    we're just skipping the doomed fetch + enqueue. "Known" = we have a
    follower relationship with, or a prior logged inbound activity from,
    that actor; a known-actor self-activity still falls through to the
    normal verified path so its audit trail is preserved.
    """
    if activity.get("type") not in ("Delete", "Update"):
        return False
    actor = activity.get("actor")
    if not isinstance(actor, str) or not actor:
        return False
    obj = activity.get("object")
    obj_id = obj.get("id") if isinstance(obj, dict) else obj
    if actor != obj_id:
        return False
    known = (
        FederationFollower.objects.filter(actor_uri=actor).exists()
        or FederationActivity.objects.filter(
            direction=DIRECTION_INBOUND, actor_uri=actor
        ).exists()
    )
    return not known


def enqueue_inbound_activity(
    *,
    actor_kind: str,
    identifier: str,
    method: str,
    path: str,
    headers: dict,
    body: bytes,
) -> None:
    """Schedule verify+process on the qcluster worker (or run in-band).

    ``actor_kind`` is ``"person"`` or ``"company"``; ``identifier`` is the
    username or company slug the POST targeted. ``headers`` / ``body`` are
    the raw request headers dict + body bytes needed to re-verify the
    signature off the web thread.

    When ``ACTIVITYPUB_INBOX_DISPATCH_SYNC`` is truthy the worker runs
    in-band (default False in prod → real ``async_task``; defaulted True
    under TESTING so the inbox suite observes side effects synchronously).
    Unlike ``Q_CLUSTER['sync']`` this knob affects ONLY the inbox path.
    """
    if getattr(settings, "ACTIVITYPUB_INBOX_DISPATCH_SYNC", False):
        process_inbound_activity(
            actor_kind=actor_kind,
            identifier=identifier,
            method=method,
            path=path,
            headers=headers,
            body=body,
        )
        return

    from django_q.tasks import async_task

    async_task(
        _TASK_PATH,
        actor_kind=actor_kind,
        identifier=identifier,
        method=method,
        path=path,
        headers=headers,
        body=body,
    )


def process_inbound_activity(
    *,
    actor_kind: str,
    identifier: str,
    method: str,
    path: str,
    headers: dict,
    body,
) -> None:
    """qcluster worker: verify the signature, then dispatch by type.

    Re-entrant + exception-safe: never raises (a bad activity must not
    crash the worker, and — under the sync knob — must not surface as a
    500 in the enqueuing view). All expensive network I/O (bounded,
    negatively cached remote key fetch; Accept delivery; peer endpoint
    lookup for Follow) happens here, off the web thread.
    """
    try:
        if isinstance(body, str):
            body = body.encode("utf-8")

        try:
            activity = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            log.info(
                "ap.inbox.worker.malformed_json kind=%s id=%s", actor_kind, identifier
            )
            return
        if not isinstance(activity, dict):
            log.info("ap.inbox.worker.not_object kind=%s id=%s", actor_kind, identifier)
            return

        replay = _ReplayRequest(method=method, path=path, headers=headers or {})
        try:
            verified = federation_signing.verify_inbound_signature(replay, body)
        except federation_signing.SignatureVerificationError as exc:
            # Accept-then-drop: the peer already received a 202. Log the
            # verdict so the 401 causes can be segmented (self-Delete of a
            # gone actor vs. unreachable keyId host vs. signature mismatch)
            # — the by-cause instrumentation CC-127 asks for.
            log.info(
                "ap.inbox.worker.unverified verdict=%s kind=%s id=%s actor=%s",
                exc.verdict,
                actor_kind,
                identifier,
                str(activity.get("actor") or "")[:200],
            )
            return

        # Lazy import — the handlers + ensure-helpers live in the views
        # module, which imports this module for the edge enqueue. Importing
        # at call time keeps that cycle from resolving at load.
        from job_hunting.api.views import federation as fed

        if actor_kind == "company":
            company = Company.objects.filter(slug=identifier).first()
            if company is None:
                log.info("ap.inbox.worker.company_gone slug=%s", identifier)
                return
            actor = fed._ensure_keypair(fed._ensure_company_actor(company))
            fed.dispatch_verified_company_activity(activity, company, actor, verified)
        else:
            actor = Actor.objects.filter(preferred_username=identifier).first()
            if actor is None:
                log.info("ap.inbox.worker.actor_gone username=%s", identifier)
                return
            actor = fed._ensure_keypair(actor)
            fed.dispatch_verified_person_activity(activity, actor, verified)
    except Exception:  # pragma: no cover - defensive; worker must not die
        log.exception(
            "ap.inbox.worker.unexpected kind=%s id=%s", actor_kind, identifier
        )
