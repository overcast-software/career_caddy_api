"""Username policy for the Phase 2.5 catchall mail ingest.

`<username>@careercaddy.online` is the catchall mailbox the email
poller listens on. The local-part — the username — must be safe for
SMTP routing, which is a stricter constraint than Django's default
UsernameField allows (Django permits `@`, `+`, spaces in some
configurations). Without enforcement at the API boundary, a user
could sign up as `foo+bar` and later have their catchall mail
silently misdelivered.

Rule: lowercase ASCII letters, digits, dot, underscore, or hyphen.
At least one character. No leading/trailing dot or hyphen (RFC 5321
forbids these at the boundaries of a local-part); no consecutive dots
(also RFC 5321). Length cap matches `User.username`'s 150-char
Django default.

This module is plain functions (no Django serializer / form dep) so
the management command (`audit_usernames`) and the API serializers
share one source of truth.
"""

from __future__ import annotations

import re

# Lowercase ASCII alphanum plus `._-` per the spec. The full rule
# (including consecutive-dot and edge-dot/hyphen prohibitions) lives
# in `is_valid_username`; this regex captures only the character-class
# half so the error message and the audit command can both point at
# the simple "wrong characters" failure mode.
_USERNAME_CHARSET_RE = re.compile(r"^[a-z0-9._-]+$")
USERNAME_MAX_LENGTH = 150


class UsernamePolicyError(ValueError):
    """Raised when a username violates the catchall policy."""


def is_valid_username(username: str) -> bool:
    """Cheap predicate. Use validate_username when you also want a reason."""
    try:
        validate_username(username)
    except UsernamePolicyError:
        return False
    return True


def validate_username(username: str) -> str:
    """Return the username if valid; raise UsernamePolicyError otherwise.

    The returned string is the input unchanged — this validator does
    NOT auto-lowercase or auto-strip. Coercion is the caller's job
    (e.g. the signup form lowercases before submit per the frontend
    half of the spec). Silent coercion here would let a typo slip
    through ("FooBar" → "foobar" → mismatched login on next sign-in).
    """
    if not isinstance(username, str):
        raise UsernamePolicyError("Username must be a string")
    if not username:
        raise UsernamePolicyError("Username is required")
    if len(username) > USERNAME_MAX_LENGTH:
        raise UsernamePolicyError(
            f"Username may not exceed {USERNAME_MAX_LENGTH} characters"
        )
    if not _USERNAME_CHARSET_RE.match(username):
        raise UsernamePolicyError(
            "Username must contain only lowercase letters, digits, "
            "dot, underscore, or hyphen"
        )
    # RFC 5321 local-part rules: no edge dots, no consecutive dots.
    # Hyphen edges are conventional rather than RFC-mandated, but
    # most mail-providers reject them — match conservatively here.
    if username[0] in ".-" or username[-1] in ".-":
        raise UsernamePolicyError(
            "Username may not start or end with '.' or '-'"
        )
    if ".." in username:
        raise UsernamePolicyError("Username may not contain consecutive dots")
    return username
