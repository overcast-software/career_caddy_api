"""Phase 5c HTTP Signatures — sign / verify / deliver tests.

Pins the cavage-12 HTTP Signature implementation that the inbox view
depends on. Tests build a FakeRemoteActor (RSA-2048 keypair) per case,
monkeypatch ``fetch_actor_public_key`` so the verifier sees a known
PEM, and exercise both the happy path and every individual failure
verdict the inbox handler depends on for its 401 mapping.

The point is to isolate signing logic from the inbox view — if a test
here fails, the inbox tests don't have to disentangle whether the
signature math or the dispatch logic is broken.
"""
from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import RequestFactory, TestCase, override_settings

from job_hunting.lib import federation_signing
from job_hunting.lib.federation_signing import (
    SignatureVerificationError,
    compute_digest_header,
    sign_outbound_post,
    verify_inbound_signature,
)


def _gen_keypair() -> tuple[str, str]:
    """Return (private_pem, public_pem) for a fresh RSA-2048 keypair."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return priv, pub


class FakeRemoteActor:
    """Stand-in for a remote AP actor: keypair + signing helper."""

    def __init__(self, actor_uri: str = "https://peer.example/users/alice"):
        self.actor_uri = actor_uri
        self.private_pem, self.public_pem = _gen_keypair()

    @property
    def key_id(self) -> str:
        return f"{self.actor_uri}#main-key"

    def sign_request(
        self,
        method: str,
        path: str,
        body: bytes,
        host: str,
        *,
        date: str | None = None,
        digest: str | None = None,
        signed_headers: list[str] | None = None,
    ) -> dict[str, str]:
        """Build cavage-12 signed headers from this fake actor's key."""
        if date is None:
            date = format_datetime(datetime.now(tz=timezone.utc), usegmt=True)
        if digest is None:
            digest = compute_digest_header(body)
        if signed_headers is None:
            signed_headers = ["(request-target)", "host", "date", "digest"]

        lines = []
        for name in signed_headers:
            lower = name.lower()
            if lower == "(request-target)":
                lines.append(f"(request-target): {method.lower()} {path}")
            elif lower == "host":
                lines.append(f"host: {host}")
            elif lower == "date":
                lines.append(f"date: {date}")
            elif lower == "digest":
                lines.append(f"digest: {digest}")
        signed_string = "\n".join(lines).encode("utf-8")

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        priv = serialization.load_pem_private_key(
            self.private_pem.encode("utf-8"), password=None
        )
        signature = priv.sign(signed_string, padding.PKCS1v15(), hashes.SHA256())
        sig_b64 = base64.b64encode(signature).decode("ascii")

        sig_header = (
            f'keyId="{self.key_id}",'
            f'algorithm="rsa-sha256",'
            f'headers="{" ".join(signed_headers)}",'
            f'signature="{sig_b64}"'
        )
        return {
            "Host": host,
            "Date": date,
            "Digest": digest,
            "Signature": sig_header,
        }


def _patch_peer_key(peer: FakeRemoteActor):
    """Context manager: monkeypatch fetch_actor_public_key + cache."""
    return patch.object(
        federation_signing,
        "fetch_actor_public_key",
        return_value=peer.public_pem,
    )


def _make_post_request(path: str, body: bytes, headers: dict[str, str]):
    """Build a Django HttpRequest mimicking the inbox POST."""
    factory = RequestFactory()
    # RequestFactory's headers kwarg replaces HTTP_* on Django 4.2+;
    # convert dict to HTTP_X env keys so older shims also work.
    meta = {}
    for k, v in headers.items():
        if k.lower() == "host":
            meta["HTTP_HOST"] = v
            meta["SERVER_NAME"] = v
        else:
            meta[f"HTTP_{k.upper().replace('-', '_')}"] = v
    return factory.post(
        path, data=body, content_type="application/activity+json", **meta
    )


class TestVerifyInbound(TestCase):
    """Verifier happy paths + per-verdict failure modes."""

    def setUp(self):
        self.peer = FakeRemoteActor()
        self.path = "/actors/dough/inbox"
        self.host = "api.careercaddy.online"
        self.body = b'{"id":"https://peer.example/activities/1","type":"Follow"}'

    def test_valid_signature_passes(self):
        headers = self.peer.sign_request("POST", self.path, self.body, self.host)
        request = _make_post_request(self.path, self.body, headers)
        with _patch_peer_key(self.peer):
            verified = verify_inbound_signature(request, self.body)
        self.assertEqual(verified.actor_uri, self.peer.actor_uri)
        self.assertIn("(request-target)", verified.signed_headers)
        self.assertEqual(verified.signature_header, headers["Signature"])

    def test_tampered_body_rejected(self):
        headers = self.peer.sign_request("POST", self.path, self.body, self.host)
        tampered = self.body + b" "  # changes digest
        request = _make_post_request(self.path, tampered, headers)
        with _patch_peer_key(self.peer):
            with self.assertRaises(SignatureVerificationError) as ctx:
                verify_inbound_signature(request, tampered)
        self.assertEqual(ctx.exception.verdict, "digest_mismatch")

    def test_tampered_signed_headers_rejected(self):
        # Sign a 4-header set, then tell the server we signed a
        # different set — the verifier rebuilds the signed string from
        # the headers list and the signature won't match.
        headers = self.peer.sign_request("POST", self.path, self.body, self.host)
        # Mutate Signature header's ``headers`` parameter
        mutated_sig = headers["Signature"].replace(
            'headers="(request-target) host date digest"',
            'headers="(request-target) date digest"',
        )
        headers["Signature"] = mutated_sig
        request = _make_post_request(self.path, self.body, headers)
        with _patch_peer_key(self.peer):
            with self.assertRaises(SignatureVerificationError) as ctx:
                verify_inbound_signature(request, self.body)
        # Missing required signed header bails before crypto
        self.assertEqual(ctx.exception.verdict, "incomplete_signed_headers")

    def test_expired_date_rejected(self):
        # 30min in the past — outside the 5min default window.
        old = format_datetime(
            datetime.now(tz=timezone.utc) - timedelta(minutes=30), usegmt=True
        )
        headers = self.peer.sign_request(
            "POST", self.path, self.body, self.host, date=old
        )
        request = _make_post_request(self.path, self.body, headers)
        with _patch_peer_key(self.peer):
            with self.assertRaises(SignatureVerificationError) as ctx:
                verify_inbound_signature(request, self.body)
        self.assertEqual(ctx.exception.verdict, "stale_date_header")

    def test_missing_signature_header_rejected(self):
        headers = {"Host": self.host, "Date": format_datetime(
            datetime.now(tz=timezone.utc), usegmt=True,
        ), "Digest": compute_digest_header(self.body)}
        request = _make_post_request(self.path, self.body, headers)
        with self.assertRaises(SignatureVerificationError) as ctx:
            verify_inbound_signature(request, self.body)
        self.assertEqual(ctx.exception.verdict, "missing_signature_header")

    def test_missing_digest_header_rejected(self):
        # Sign over (request-target)/host/date only, omit digest
        headers = self.peer.sign_request(
            "POST", self.path, self.body, self.host,
            signed_headers=["(request-target)", "host", "date"],
        )
        # Drop Digest header from the request entirely
        headers.pop("Digest", None)
        request = _make_post_request(self.path, self.body, headers)
        with _patch_peer_key(self.peer):
            with self.assertRaises(SignatureVerificationError) as ctx:
                verify_inbound_signature(request, self.body)
        # Required-header check rejects before signature math
        self.assertEqual(ctx.exception.verdict, "incomplete_signed_headers")

    def test_malformed_signature_header(self):
        headers = self.peer.sign_request("POST", self.path, self.body, self.host)
        headers["Signature"] = 'garbage="value"'  # missing keyId
        request = _make_post_request(self.path, self.body, headers)
        with self.assertRaises(SignatureVerificationError) as ctx:
            verify_inbound_signature(request, self.body)
        self.assertEqual(ctx.exception.verdict, "malformed_signature_header")

    def test_signature_with_wrong_key_rejected(self):
        # Two keypairs: sign with one, verify against the other.
        other = FakeRemoteActor(actor_uri=self.peer.actor_uri)
        headers = self.peer.sign_request("POST", self.path, self.body, self.host)
        request = _make_post_request(self.path, self.body, headers)
        with patch.object(
            federation_signing,
            "fetch_actor_public_key",
            return_value=other.public_pem,
        ):
            with self.assertRaises(SignatureVerificationError) as ctx:
                verify_inbound_signature(request, self.body)
        self.assertEqual(ctx.exception.verdict, "signature_mismatch")


class FakeLocalActor:
    """Stand-in for our local Actor model in signing-only tests."""

    def __init__(self, preferred_username: str = "dough"):
        self.preferred_username = preferred_username
        self.private_key_pem, self.public_key_pem = _gen_keypair()


@override_settings(INSTANCE_ORIGIN="https://careercaddy.online")
class TestSignOutboundRoundTrip(TestCase):
    """Sign outbound, then verify with the matching public key."""

    def test_round_trip_with_own_keypair(self):
        local = FakeLocalActor("dough")
        body = b'{"type":"Accept"}'
        url = "https://peer.example/users/alice/inbox"
        headers = sign_outbound_post(url, body, local)

        # The signature header should round-trip through the verifier
        # when we present our own public key as if we were the peer.
        path = "/users/alice/inbox"
        request = _make_post_request(path, body, headers)
        with patch.object(
            federation_signing,
            "fetch_actor_public_key",
            return_value=local.public_key_pem,
        ):
            verified = verify_inbound_signature(request, body)
        self.assertEqual(verified.actor_uri, "https://careercaddy.online/actors/dough")
        self.assertEqual(headers["Content-Type"], "application/activity+json")

    def test_sign_outbound_includes_required_headers(self):
        local = FakeLocalActor("dough")
        headers = sign_outbound_post(
            "https://peer.example/users/alice/inbox",
            b"{}",
            local,
        )
        for required in ("Host", "Date", "Digest", "Signature", "Content-Type"):
            self.assertIn(required, headers)
        # The signature MUST cover all four required headers
        sig_value = headers["Signature"]
        self.assertIn("(request-target)", sig_value)
        self.assertIn("host", sig_value)
        self.assertIn("date", sig_value)
        self.assertIn("digest", sig_value)


class TestDigestHelper(TestCase):
    """compute_digest_header is its own pinned contract — many callers rely on it."""

    def test_digest_format(self):
        body = b"hello world"
        header = compute_digest_header(body)
        self.assertTrue(header.startswith("SHA-256="))
        # Decode the base64 portion and verify it matches sha256
        digest_b64 = header.split("=", 1)[1]
        decoded = base64.b64decode(digest_b64)
        self.assertEqual(decoded, hashlib.sha256(body).digest())
