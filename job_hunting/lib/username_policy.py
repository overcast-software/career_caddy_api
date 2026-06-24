"""Username policy — one rule, two jobs.

The username is load-bearing in two places, so it has to be safe for
both:

1. **Catchall mail (Phase 2.5).** `<username>@careercaddy.online` is the
   catchall mailbox the email poller listens on, so the username is an
   SMTP local-part.
2. **Public ActivityPub actor handle (CC-56 #58/#59).** The username
   becomes `@user`, the WebFinger `acct:user@careercaddy.online`, and
   the `/@user` URL. Federated consumers — Mastodon being the dominant
   one (the live target is `@dough` on mstdn.social) — restrict an
   actor `preferredUsername` to `[A-Za-z0-9_]`. Dot and hyphen are
   *valid* in an email local-part but *not* in a Mastodon handle.

Rule (the safe intersection of both): **lowercase ASCII letters,
digits, and underscore only**; length **>= 3** (the actor-handle floor)
and **<= 150** (Django's `User.username` default). At least one
character, no auto-coercion.

Both the proposed length floor (3) and the charset (`[a-z0-9_]`) are
cc-api proposals pending Doug's confirmation (CC-56 #58/#59). They are
single-constant / single-regex knobs so the policy is cheap to retune
(e.g. re-admitting `.`/`-` for an email-only rationale is a one-line
revert of the regex).

This module is plain functions (no Django serializer / form dep) so the
management command (`audit_usernames`) and the API write paths share one
source of truth.
"""

from __future__ import annotations

import re

# Lowercase ASCII alphanumerics plus underscore — the intersection of a
# safe SMTP local-part and a Mastodon-safe actor handle. Tightened from
# the earlier catchall-only `[a-z0-9._-]`: dot and hyphen are dropped
# because they are invalid in a federated actor handle (CC-56 #59).
_USERNAME_CHARSET_RE = re.compile(r"^[a-z0-9_]+$")

# Actor-handle floor (CC-56 #58). Proposed >= 3 pending Doug; bump this
# one constant to retune. Below this, `@ab`-style handles are too short
# to be safe/meaningful public identifiers.
USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 150


class UsernamePolicyError(ValueError):
    """Raised when a username violates the policy."""


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
    through ("FooBar" -> "foobar" -> mismatched login on next sign-in).
    """
    if not isinstance(username, str):
        raise UsernamePolicyError("Username must be a string")
    if not username:
        raise UsernamePolicyError("Username is required")
    if len(username) < USERNAME_MIN_LENGTH:
        raise UsernamePolicyError(
            f"Username must be at least {USERNAME_MIN_LENGTH} characters"
        )
    if len(username) > USERNAME_MAX_LENGTH:
        raise UsernamePolicyError(
            f"Username may not exceed {USERNAME_MAX_LENGTH} characters"
        )
    if not _USERNAME_CHARSET_RE.match(username):
        raise UsernamePolicyError(
            "Username must contain only lowercase letters, digits, "
            "or underscore"
        )
    return username
