"""HTTP Signatures — verify inbound, sign outbound, deliver.

Phase 5c of Plans/ActivityPub Phase 5 — federation proper. Implements
the Mastodon-compatible cavage-12 (``draft-cavage-http-signatures-12``)
flavour of HTTP Signatures. The newer RFC 9421 spec is on Mastodon's
roadmap but cavage-12 is what the fediverse actually speaks today.

The signed string is constructed deterministically from the header
list in the ``Signature`` header's ``headers="..."`` parameter:

    (request-target): post /actors/dough/inbox
    host: api.careercaddy.online
    date: Sun, 01 Jun 2026 12:34:56 GMT
    digest: SHA-256=base64(sha256(body))

Each header is lowercased, value-trimmed, and joined with ``\n``. The
result is RSA-SHA256 signed; the signature is base64-encoded into the
``signature="..."`` parameter.

Public-key fetch is cached for ``ACTIVITYPUB_PEER_KEY_CACHE_TTL``
seconds in Django's cache backend, keyed by the actor URI prefix of
``keyId`` (``<actor>#main-key`` → cache key ``actor``). The cache is
local-memory by default; if you need cross-process key cache for prod
fan-in, configure ``CACHES`` to use Redis/Memcached.

Public API:

- ``verify_inbound_signature(request)`` — returns ``VerifiedSignature``
  on success, raises ``SignatureVerificationError`` with a verdict
  string on failure.
- ``sign_outbound_post(url, body, local_actor)`` — returns the headers
  dict ready to attach to a request.
- ``deliver(url, body, local_actor)`` — POSTs + signs, returns
  ``(status_code, body_snippet)``.

This module is import-safe in tests — no network calls happen on
import; ``fetch_actor_public_key`` and ``deliver`` are explicit entry
points so tests can monkeypatch them.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from django.conf import settings
from django.core.cache import cache


AS2_CONTENT_TYPE = "application/activity+json"

# Required header set for inbound POST signatures. Mastodon signs all
# four (plus host computed from the URL); any missing required header
# means we can't be sure the body / target / time wasn't tampered with.
REQUIRED_SIGNED_HEADERS_POST = {"(request-target)", "host", "date", "digest"}


class SignatureVerificationError(Exception):
    """Raised when an inbound HTTP signature fails verification.

    ``verdict`` is a short string suitable for logging (e.g.
    ``"missing_signature_header"``, ``"digest_mismatch"``). The inbox
    handler maps verdict → response so the caller doesn't have to
    parse exception messages.
    """

    def __init__(self, verdict: str, detail: str = "") -> None:
        self.verdict = verdict
        self.detail = detail
        super().__init__(f"{verdict}: {detail}" if detail else verdict)


@dataclass
class VerifiedSignature:
    """Outcome of a successful inbound signature verification."""

    key_id: str
    actor_uri: str
    signed_headers: list[str]
    signature_header: str  # raw ``Signature:`` header verbatim for audit


# --- Signature header parsing ------------------------------------------

# ``Signature: keyId="...",algorithm="...",headers="...",signature="..."``
# Values are quoted-strings; the spec doesn't permit escapes inside
# them. A simple regex is enough — we deliberately do NOT use a full
# RFC 7235 parser because cavage-12 servers in the wild are lenient.
_SIG_PARAM_RE = re.compile(r'(\w+)="([^"]*)"')


def _parse_signature_header(value: str) -> dict[str, str]:
    """Split the ``Signature`` header into its quoted parameters."""
    return dict(_SIG_PARAM_RE.findall(value))


# --- Date / Digest helpers ---------------------------------------------


def _parse_date_header(value: str) -> datetime:
    """Parse an HTTP Date header into an aware datetime.

    Mastodon emits ``Sun, 01 Jun 2026 12:34:56 GMT`` (RFC 7231 §7.1.1.1).
    ``parsedate_to_datetime`` handles the spec format; the result is
    forced to UTC so our ±window comparison doesn't drift on naive
    timestamps.
    """
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError) as exc:
        raise SignatureVerificationError("bad_date_header", str(exc)) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _date_within_window(date_value: str) -> bool:
    """Return True iff ``Date`` is within ``ACTIVITYPUB_DATE_WINDOW_SECONDS`` of now."""
    window = getattr(settings, "ACTIVITYPUB_DATE_WINDOW_SECONDS", 300)
    dt = _parse_date_header(date_value)
    now = datetime.now(tz=timezone.utc)
    return abs((now - dt).total_seconds()) <= window


def compute_digest_header(body: bytes) -> str:
    """Return the ``Digest`` header value for ``body`` (SHA-256, base64)."""
    sha = hashlib.sha256(body).digest()
    return "SHA-256=" + base64.b64encode(sha).decode("ascii")


def _verify_digest(header_value: str, body: bytes) -> None:
    """Raise on Digest header mismatch.

    Spec allows multiple digest algs comma-separated; in practice
    Mastodon emits SHA-256 only. We accept any SHA-256 entry and
    ignore others (forward-compat with SHA-512 if a peer ships it).
    """
    if not header_value:
        raise SignatureVerificationError("missing_digest")
    expected = compute_digest_header(body)
    for entry in header_value.split(","):
        if entry.strip() == expected:
            return
    raise SignatureVerificationError("digest_mismatch")


# --- Signed-string construction ----------------------------------------


def _build_signed_string(
    method: str,
    path: str,
    headers: dict[str, str],
    signed_header_names: list[str],
) -> bytes:
    """Construct the cavage-12 signed string from ``headers``.

    Each line is ``<lowercased name>: <value>`` joined by ``\\n``. The
    pseudo-header ``(request-target)`` is computed from method + path.
    Missing headers raise — the verifier shouldn't reach here unless
    the required-header check already passed.
    """
    lines = []
    for name in signed_header_names:
        lower = name.lower()
        if lower == "(request-target)":
            lines.append(f"(request-target): {method.lower()} {path}")
        else:
            if lower not in headers:
                raise SignatureVerificationError(
                    "missing_signed_header", f"signed header {name!r} not in request"
                )
            lines.append(f"{lower}: {headers[lower]}")
    return "\n".join(lines).encode("utf-8")


# --- Peer key fetch (cached) -------------------------------------------


def fetch_actor_public_key(actor_uri: str) -> str:
    """Fetch the remote Actor JSON-LD and return its ``publicKey.publicKeyPem``.

    Caches the PEM for ``ACTIVITYPUB_PEER_KEY_CACHE_TTL`` seconds keyed
    by actor URI. Tests monkeypatch this function with a fake; never
    called in the unit-test path. Raises
    ``SignatureVerificationError`` on any failure so the inbox handler
    can map verdict → 401 uniformly.

    Network safety (CC-127): a split connect/read timeout
    (``ACTIVITYPUB_PEER_KEY_FETCH_CONNECT_TIMEOUT`` 3s /
    ``ACTIVITYPUB_PEER_KEY_FETCH_READ_TIMEOUT`` 10s) fails a DEAD host
    fast in the connect phase while still letting a slow-but-ALIVE legit
    peer respond, a low redirect cap
    (``ACTIVITYPUB_PEER_KEY_FETCH_MAX_REDIRECTS``, default 3 — a bare
    ``requests`` default of 30 lets a redirect-looping peer multiply the
    timeout into the 40s range), and a 256KB response cap keep a slow /
    hostile / redirect-looping peer from pinning the caller. The fetch
    runs in the qcluster worker (not a web thread) so the read budget can
    match Mastodon's 10s rather than the brutal 2-3s a web-thread verify
    would need.

    Failures are NEGATIVELY cached for
    ``ACTIVITYPUB_PEER_KEY_NEG_CACHE_TTL`` seconds so an ActivityPub
    redelivery storm from a dead / deleted actor doesn't re-pay the full
    network cost on every retry. This is the dominant CC-127 401 source:
    self-``Delete`` broadcasts from suspended fediverse accounts + dead
    peers whose key can no longer be fetched — inherently unverifiable,
    and previously re-fetched (uncached) on every backoff redelivery.
    """
    cache_key = f"ap:pubkey:{actor_uri}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    # Negative cache: a recent fetch failure short-circuits before any
    # network I/O so the redelivery storm stays cheap.
    neg_key = f"ap:pubkey_neg:{actor_uri}"
    neg = cache.get(neg_key)
    if neg:
        raise SignatureVerificationError(neg, "negative-cached")

    def _fail(verdict: str) -> SignatureVerificationError:
        neg_ttl = getattr(settings, "ACTIVITYPUB_PEER_KEY_NEG_CACHE_TTL", 600)
        cache.set(neg_key, verdict, neg_ttl)
        return SignatureVerificationError(verdict)

    connect_timeout = getattr(
        settings, "ACTIVITYPUB_PEER_KEY_FETCH_CONNECT_TIMEOUT", 3.0
    )
    read_timeout = getattr(
        settings, "ACTIVITYPUB_PEER_KEY_FETCH_READ_TIMEOUT", 10.0
    )
    max_redirects = getattr(
        settings, "ACTIVITYPUB_PEER_KEY_FETCH_MAX_REDIRECTS", 3
    )
    session = requests.Session()
    session.max_redirects = max_redirects
    try:
        try:
            response = session.get(
                actor_uri,
                headers={"Accept": AS2_CONTENT_TYPE},
                timeout=(connect_timeout, read_timeout),
                stream=True,
            )
        except requests.RequestException as exc:
            # Covers ConnectTimeout / ReadTimeout / TooManyRedirects / DNS.
            raise _fail("peer_unreachable") from exc

        if response.status_code != 200:
            raise _fail("peer_actor_fetch_failed")

        # Cap response read at 256KB — public Mastodon actor JSON is ~3KB.
        try:
            body = response.raw.read(256 * 1024, decode_content=True)
        except Exception as exc:  # pragma: no cover - defensive
            raise _fail("peer_actor_read_failed") from exc
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _fail("peer_actor_malformed") from exc

        public_key = (data or {}).get("publicKey") or {}
        pem = public_key.get("publicKeyPem")
        if not pem:
            raise _fail("peer_no_public_key")
    finally:
        session.close()

    ttl = getattr(settings, "ACTIVITYPUB_PEER_KEY_CACHE_TTL", 300)
    cache.set(cache_key, pem, ttl)
    return pem


# --- Inbound verification ----------------------------------------------


def _normalised_headers(django_request) -> dict[str, str]:
    """Return a lowercase-keyed copy of the request's HTTP headers."""
    return {key.lower(): value for key, value in django_request.headers.items()}


def _key_id_to_actor_uri(key_id: str) -> str:
    """Extract the actor URI prefix of ``keyId``.

    Mastodon shapes the keyId as ``<actor_uri>#main-key`` but other AP
    servers (Pleroma, GoToSocial, Lemmy) drop the fragment or use a
    different anchor. Stripping at ``#`` covers all variants without
    enumerating them.
    """
    return key_id.split("#", 1)[0]


def _precheck_inbound_signature(
    django_request, body: bytes
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Run the cheap, NETWORK-FREE half of inbound verification.

    Checks required signature params, the signed-header set, the Date
    window, and the Digest — everything that can be validated from the
    request + body alone, with no remote key fetch. Raises
    ``SignatureVerificationError`` on any cheap failure. Returns
    ``(normalised_headers, sig_params, signed_header_names)`` for the
    caller to continue into the expensive fetch + RSA leg.

    CC-127 split this out so the inbox *edge* can reject obviously-bad
    requests (missing sig, stale Date, bad Digest) with 401 cheaply —
    without spending a queue slot — while the expensive remote-key-fetch
    + RSA verify is deferred to the async worker off the web thread.
    """
    headers = _normalised_headers(django_request)

    sig_value = headers.get("signature")
    if not sig_value:
        raise SignatureVerificationError("missing_signature_header")

    params = _parse_signature_header(sig_value)
    for required in ("keyId", "headers", "signature"):
        if required not in params:
            raise SignatureVerificationError(
                "malformed_signature_header", f"missing {required}"
            )

    signed_header_names = params["headers"].split()
    signed_set = {name.lower() for name in signed_header_names}
    missing = REQUIRED_SIGNED_HEADERS_POST - signed_set
    if missing:
        raise SignatureVerificationError(
            "incomplete_signed_headers", f"missing {sorted(missing)}"
        )

    date_value = headers.get("date", "")
    if not date_value:
        raise SignatureVerificationError("missing_date_header")
    if not _date_within_window(date_value):
        raise SignatureVerificationError("stale_date_header")

    digest_value = headers.get("digest", "")
    _verify_digest(digest_value, body)

    return headers, params, signed_header_names


def verify_inbound_signature_precheck(django_request, body: bytes) -> None:
    """Public wrapper: run ONLY the cheap, network-free signature checks.

    Used by the inbox edge (CC-127 accept-then-async) to 401 tampered /
    replayed / unsigned requests before enqueuing the async verify. The
    async worker re-runs the full :func:`verify_inbound_signature`
    (cheap checks + remote key fetch + RSA) as the real trust gate.
    """
    _precheck_inbound_signature(django_request, body)


def verify_inbound_signature(django_request, body: bytes) -> VerifiedSignature:
    """Verify an inbound POST's HTTP signature + Digest + Date.

    Raises ``SignatureVerificationError`` (mapped to 401 by the caller)
    on any failure. On success returns a ``VerifiedSignature`` carrying
    the verified ``keyId`` / actor URI / signed-header list so the
    caller can audit-log + dispatch by activity type.

    Order matters: cheap header checks first, expensive crypto last. The
    cheap half lives in :func:`_precheck_inbound_signature`; the
    remote key fetch (bounded + negatively cached, see
    :func:`fetch_actor_public_key`) + RSA verify follow. CC-127 runs the
    full function only in the qcluster worker — never on a web thread.
    """
    headers, params, signed_header_names = _precheck_inbound_signature(
        django_request, body
    )

    key_id = params["keyId"]
    actor_uri = _key_id_to_actor_uri(key_id)
    public_pem = fetch_actor_public_key(actor_uri)

    try:
        public_key = serialization.load_pem_public_key(public_pem.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise SignatureVerificationError("peer_key_malformed", str(exc)) from exc

    signed_string = _build_signed_string(
        method=django_request.method,
        path=django_request.path,
        headers=headers,
        signed_header_names=signed_header_names,
    )

    try:
        signature_bytes = base64.b64decode(params["signature"], validate=False)
    except (ValueError, TypeError) as exc:
        raise SignatureVerificationError("bad_signature_encoding", str(exc)) from exc

    try:
        public_key.verify(
            signature_bytes,
            signed_string,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except InvalidSignature as exc:
        raise SignatureVerificationError("signature_mismatch") from exc

    return VerifiedSignature(
        key_id=key_id,
        actor_uri=actor_uri,
        signed_headers=signed_header_names,
        signature_header=headers.get("signature", ""),
    )


# --- Outbound signing --------------------------------------------------


def _http_date_now() -> str:
    """Return current time in HTTP-date format (RFC 7231)."""
    return format_datetime(datetime.now(tz=timezone.utc), usegmt=True)


def sign_outbound_post(
    url: str,
    body: bytes,
    local_actor: Any,
    *,
    now: str | None = None,
    actor_uri: str | None = None,
) -> dict[str, str]:
    """Build signed headers for an outbound AP POST.

    ``local_actor`` must expose ``private_key_pem`` and have a
    ``preferred_username`` we can mint the actor URI from. Returns the
    headers dict (Date, Digest, Host, Content-Type, Signature) ready to
    attach to a request.

    ``actor_uri`` overrides the default ``{origin}/actors/<u>`` URI —
    Phase 6b Company actors live at ``{origin}/companies/<slug>`` so
    the keyId + audit-trail actor identity must point at that URI,
    not the Person-actor path. Caller passes the explicit URI; falls
    back to the Person-actor builder for Phase 5c parity.

    ``now`` is an injection seam for tests that want a stable Date.
    """
    parsed = urlparse(url)
    host = parsed.netloc
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    date = now or _http_date_now()
    digest = compute_digest_header(body)

    if actor_uri is None:
        actor_uri = _local_actor_uri(local_actor)
    key_id = f"{actor_uri}#main-key"
    headers_list = ["(request-target)", "host", "date", "digest"]

    signed_string = _build_signed_string(
        method="post",
        path=path,
        headers={"host": host, "date": date, "digest": digest},
        signed_header_names=headers_list,
    )

    private_key = serialization.load_pem_private_key(
        local_actor.private_key_pem.encode("utf-8"),
        password=None,
    )
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise ValueError("local_actor.private_key_pem must be RSA")
    signature = private_key.sign(
        signed_string,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    signature_b64 = base64.b64encode(signature).decode("ascii")

    signature_header = (
        f'keyId="{key_id}",'
        f'algorithm="rsa-sha256",'
        f'headers="{" ".join(headers_list)}",'
        f'signature="{signature_b64}"'
    )

    return {
        "Host": host,
        "Date": date,
        "Digest": digest,
        "Content-Type": AS2_CONTENT_TYPE,
        "Signature": signature_header,
    }


def _local_actor_uri(local_actor: Any) -> str:
    """Mint the local actor URI from settings + the actor's preferred_username.

    Duplicates the helper in ``views/federation.py`` to keep the
    signing module dependency-free from the views layer (otherwise a
    circular import lurks once ``views/federation.py`` imports
    ``federation_signing``).
    """
    origin = settings.INSTANCE_ORIGIN.rstrip("/")
    return f"{origin}/actors/{local_actor.preferred_username}"


def deliver(
    url: str,
    body: bytes,
    local_actor: Any,
    *,
    timeout: float | None = None,
    actor_uri: str | None = None,
) -> tuple[int, str]:
    """POST a signed activity to ``url``.

    Returns ``(status_code, body_snippet)``. ``status_code == 0`` on
    network error; ``body_snippet`` is the error class name + message
    in that case. Caller decides retry policy — Phase 5c V1 is
    one-shot, 5d's dispatcher will layer retries on top.

    ``actor_uri`` is forwarded to :func:`sign_outbound_post` so the
    Company-actor (``/companies/<slug>``) outbound path can sign with
    its non-Person URI. Default behavior is unchanged.

    Body snippet capped at 512 chars so the audit log doesn't bloat
    on a hostile peer that returns an HTML error page.
    """
    timeout = timeout if timeout is not None else getattr(
        settings, "ACTIVITYPUB_OUTBOUND_DELIVERY_TIMEOUT", 10
    )
    headers = sign_outbound_post(url, body, local_actor, actor_uri=actor_uri)
    try:
        response = requests.post(url, data=body, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        return 0, f"{type(exc).__name__}: {exc}"
    snippet = (response.text or "")[:512]
    return response.status_code, snippet
