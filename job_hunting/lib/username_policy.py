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


# --- Reserved usernames (CC-123) -------------------------------------
#
# The username shares a namespace with four real surfaces, so a name
# that matches one of them is not merely ugly — it breaks something:
#
# 1. **Frontend routes.** `frontend/app/router.js` registers the public
#    profile page as `this.route('profile', { path: '/:username' })` —
#    a single dynamic segment sitting beside every static top-level
#    route. route-recognizer ranks static above dynamic, so a user
#    named `settings` does not hijack `/settings`; they simply have no
#    reachable profile page.
# 2. **Mail.** `<username>@careercaddy.online` is a catchall mailbox.
#    `forwarding` is the live catchall *sink* local-part
#    (`automation/src/email_source/mime.py`), and the ingest pipeline
#    discards any recipient equal to it — so a user named `forwarding`
#    would have every message addressed to them silently dropped.
# 3. **Same-origin API/actor paths.** Production nginx path-routes
#    `/api`, `/api/v1/events` and `/mcp` on the apex, and Django serves
#    `/admin/`, `/actors/<username>/`, `/companies/<slug>/` and
#    `/job-posts/<pk>` at the root (`job_hunting/urls.py`).
# 4. **Internal accounts resolved by literal username.** Some server
#    code looks a user up *by name* and grants that row special
#    standing. `guest` is the worst case: `_guest_user_id()`
#    (`api/views/reports.py`) resolves `username="guest"` and
#    `application_flow_report` — `@permission_classes([AllowAny])` —
#    serves that user's whole job-post + application pipeline to
#    unauthenticated callers. On an install where the demo account has
#    not been seeded yet, whoever registers `guest` first has their
#    real pipeline published anonymously. It also bricks demo mode:
#    `seed_guest` skips creation when the row exists, so
#    `/api/v1/guest-session/` then 400s "Guest account not configured"
#    forever with no way to reclaim the name.
#
# Each group below is the literal set of names taken from those
# surfaces. **To extend: add the new frontend route / API resource to
# its group.** Hyphenated entries are already unreachable under the
# `[a-z0-9_]` charset; they are kept so the list stays complete and
# mechanical to maintain if the charset is ever retuned (the module
# docstring notes re-admitting `-` is a one-line change).

# Every top-level route registered in frontend/app/router.js Router.map.
_FRONTEND_TOP_LEVEL_ROUTES = frozenset({
    "about", "accept-invite", "admin", "answers", "caddy", "career-data",
    "companies", "cover-letters", "docs", "extension", "favorites",
    "forgot-password", "get-started", "job-applications", "job-posts",
    "login", "logout", "not-found", "profile", "questions", "reports",
    "reset-password", "resumes", "scores", "scrapes", "settings",
    "setup", "signup", "summaries", "users", "waitlist", "wizard",
})

# Every JSON:API resource registered on the DRF router in
# job_hunting/urls.py (the `/api/v1/<name>` collection names).
_API_RESOURCES = frozenset({
    "ai-usages", "answers", "api-keys", "certifications", "companies",
    "cover-letters", "descriptions", "educations", "experiences",
    "invitations", "job-application-statuses", "job-applications",
    "job-posts", "projects", "questions", "resumes", "scores",
    "scrape-profiles", "scrapes", "statuses", "summaries", "users",
    "waitlists",
})

# Root-level (non-`/api/v1`) paths served beside the profile route:
# Django's admin + the ActivityPub actor endpoints in urls.py, plus the
# same-origin nginx path-routes on the apex (deploy/terraform).
_ROOT_PATHS = frozenset({
    "actors", "admin", "api", "events", "mcp", "well-known",
})

# Service mailboxes. `forwarding` is the live catchall sink and
# `noreply` is the live DEFAULT_FROM_EMAIL (settings.py); the rest are
# the role addresses RFC 2142 requires a domain to keep answerable.
_SERVICE_MAILBOXES = frozenset({
    "abuse", "admin", "forwarding", "hostmaster", "mailer-daemon",
    "noreply", "no-reply", "postmaster", "security", "support",
    "webmaster",
})

# Infrastructure hostnames under careercaddy.online — reserved so an
# actor handle can never read as a service host.
_INFRA_HOSTNAMES = frozenset({
    "api", "mail", "mcp", "smtp", "www", "wiki",
})

# Names the server resolves as a literal to find a privileged row. These
# are not cosmetic collisions — registering one takes over an identity
# the code already trusts:
#   guest    — `api/views/reports.py` `_guest_user_id()` +
#              `api/views/auth.py` `guest_session`; the AllowAny
#              application-flow report publishes this user's pipeline to
#              anonymous callers, and `seed_guest` will not reclaim the
#              name once a row exists.
#   instance — the server-level ActivityPub Actor
#              (`management/commands/bootstrap_instance_actor.py`
#              INSTANCE_USERNAME). `generate_federation_actors` already
#              has to *skip* users with this name, which is the tell
#              that the collision is real.
_INTERNAL_ACCOUNTS = frozenset({
    "guest", "instance",
})

#: The single importable constant. Enforced by :func:`validate_username`
#: and therefore by every signup write path that already calls it.
RESERVED_USERNAMES = frozenset(
    _FRONTEND_TOP_LEVEL_ROUTES
    | _API_RESOURCES
    | _ROOT_PATHS
    | _SERVICE_MAILBOXES
    | _INFRA_HOSTNAMES
    | _INTERNAL_ACCOUNTS
)


class UsernamePolicyError(ValueError):
    """Raised when a username violates the policy."""


def is_valid_username(username: str, *, allow_reserved: bool = False) -> bool:
    """Cheap predicate. Use validate_username when you also want a reason."""
    try:
        validate_username(username, allow_reserved=allow_reserved)
    except UsernamePolicyError:
        return False
    return True


def validate_username(username: str, *, allow_reserved: bool = False) -> str:
    """Return the username if valid; raise UsernamePolicyError otherwise.

    The returned string is the input unchanged — this validator does
    NOT auto-lowercase or auto-strip. Coercion is the caller's job
    (e.g. the signup form lowercases before submit per the frontend
    half of the spec). Silent coercion here would let a typo slip
    through ("FooBar" -> "foobar" -> mismatched login on next sign-in).

    `allow_reserved` skips the RESERVED_USERNAMES check (CC-123). It has
    exactly one caller — the read-only `audit_usernames` command, whose
    contract is charset + length only (CC-56 #58/#59) and which must not
    report every install's pre-existing `admin` superuser as a
    violation. **No write path sets it**: every surface that creates a
    user, `POST /api/v1/initialize/` included, enforces the reserved
    list.
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
    # Last, so a name that is both malformed and reserved reports the
    # more specific charset/length reason. The charset gate above has
    # already guaranteed `username` is lowercase, so a plain membership
    # test is case-correct without coercing.
    if not allow_reserved and username in RESERVED_USERNAMES:
        raise UsernamePolicyError(
            f"Username '{username}' is reserved — it collides with a "
            "site route, an API resource, or a service address"
        )
    return username
