import os
import sys
from datetime import timedelta

import dj_database_url
from corsheaders.defaults import default_headers, default_methods
from django.core.exceptions import ImproperlyConfigured

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Environment flags and safety
SECRET_KEY = os.environ.get("SECRET_KEY", "your_generated_secret_key")
DEBUG = os.environ.get("DEBUG", "False").lower() in ("true", "1", "yes")
TESTING = any(arg in ("test", "pytest") for arg in sys.argv)

# Security check for production
if not DEBUG and (not SECRET_KEY or SECRET_KEY == "your_generated_secret_key"):
    raise ImproperlyConfigured(
        "SECRET_KEY must be set to a secure value in production. "
        "Set the SECRET_KEY environment variable."
    )

# Parse ALLOWED_HOSTS from environment (comma-separated)
ALLOWED_HOSTS_ENV = os.environ.get("ALLOWED_HOSTS", "")
if ALLOWED_HOSTS_ENV:
    ALLOWED_HOSTS = [
        host.strip() for host in ALLOWED_HOSTS_ENV.split(",") if host.strip()
    ]
else:
    # Development default
    ALLOWED_HOSTS = ["localhost", "127.0.0.1"] if DEBUG else []

# Internal docker service name — the MCP and chat sidecars reach Django via
# http://api:8000, so Django must accept "api" as a Host header or it returns
# 400 DisallowedHost before auth even runs.
if "api" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append("api")

# Ensure test-friendly hosts when testing
if TESTING:
    test_hosts = ["testserver", "localhost", "127.0.0.1"]
    for host in test_hosts:
        if host not in ALLOWED_HOSTS:
            ALLOWED_HOSTS.append(host)

# CSRF trusted origins
CSRF_TRUSTED_ORIGINS_ENV = os.environ.get("CSRF_TRUSTED_ORIGINS", "")
if CSRF_TRUSTED_ORIGINS_ENV:
    CSRF_TRUSTED_ORIGINS = [
        origin.strip()
        for origin in CSRF_TRUSTED_ORIGINS_ENV.split(",")
        if origin.strip()
    ]
else:
    CSRF_TRUSTED_ORIGINS = []

# Add frontend origins to CSRF trusted origins for cookie-based auth
if DEBUG or not CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS.extend(
        [
            "http://localhost:3000",
            "http://localhost:4200",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:4200",
            "http://localhost:8080",
            "http://127.0.0.1:8080",
        ]
    )

USE_TZ = True
# Storage is always UTC (timestamptz). TIME_ZONE is the user-facing zone —
# drives timezone.localdate() / TruncDate boundaries on activity reports so
# a 4pm-PST action lands on "today," not tomorrow-UTC. Hardcoded single-region
# for now; revisit for multi-tenant per-user tz preference.
TIME_ZONE = "America/Los_Angeles"

MIDDLEWARE = [
    "job_hunting.api.middleware.OvercastHeaderMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "job_hunting.api.middleware.ApiKeyAuthenticationMiddleware",
    "job_hunting.api.middleware.ApiKeyPermissionMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Required for postgres-specific model indexes (HashIndex on
    # JobPost.apply_url); without it Django raises postgres.E005 at
    # system-check time.
    "django.contrib.postgres",
    "rest_framework",
    "rest_framework.authtoken",
    "corsheaders",
    "drf_spectacular",
    # Async task queue — Phase 1 of the django-q2 phased rollout
    # (Plans/Job-queue integration — django-q2 phased rollout).
    # The worker container runs `manage.py qcluster`; tasks are
    # enqueued from views via async_task(). Subsequent phases migrate
    # the existing 9 daemon-thread spawn points + 2 polling daemons.
    "django_q",
    "job_hunting.apps.JobHuntingConfig",
]

ROOT_URLCONF = "job_hunting.urls"

# Database configuration with dj-database-url
DATABASE_URL = os.environ.get("DATABASE_URL")
# Persistent DB connections avoid the overhead of opening a new connection per
# request. 60s is a safe default; set CONN_MAX_AGE=0 to disable.
_conn_max_age = int(os.environ.get("CONN_MAX_AGE", "60"))
if DATABASE_URL:
    DATABASES = {"default": dj_database_url.parse(DATABASE_URL, conn_max_age=_conn_max_age)}
else:
    # Default to localhost Postgres in development
    if DEBUG:
        DATABASES = {
            "default": dj_database_url.parse(
                "postgresql://postgres:postgres@localhost:5432/job_hunting",
                conn_max_age=_conn_max_age,
            )
        }
    else:
        # In production, DATABASE_URL must be set
        raise ImproperlyConfigured(
            "DATABASE_URL environment variable must be set in production. "
            "Example: postgresql://user:password@host:port/database"
        )

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [os.path.join(BASE_DIR, "templates")],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# DRF Configuration with security hardening
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "job_hunting.api.authentication.ApiKeyAuthentication",
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "job_hunting.api.renderers.VndApiJSONRenderer",
    ]
    + (["rest_framework.renderers.BrowsableAPIRenderer"] if DEBUG else []),
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
        "job_hunting.api.parsers.VndApiJSONParser",
        "rest_framework.parsers.FormParser",
        "rest_framework.parsers.MultiPartParser",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        # Exempt trusted `jh_` service API keys (ApiKeyAuthentication) from
        # throttling; anon + JWT (frontend) traffic stays capped. See
        # job_hunting/api/throttling.py.
        "job_hunting.api.throttling.ApiKeyExemptAnonRateThrottle",
        "job_hunting.api.throttling.ApiKeyExemptUserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": os.environ.get("DRF_ANON_THROTTLE_RATE", "100/day"),
        "user": os.environ.get("DRF_USER_THROTTLE_RATE", "5000/day"),
    },
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Career Caddy API",
    "DESCRIPTION": "API for managing job applications, resumes, cover letters, and career data.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SECURITY": [{"jwtAuth": []}, {"apiKeyAuth": []}],
    "COMPONENTS": {
        "securitySchemes": {
            "jwtAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
            },
            "apiKeyAuth": {
                "type": "apiKey",
                "in": "header",
                "name": "X-Api-Key",
            },
        }
    },
}

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/3.2/howto/static-files/

STATIC_URL = "/static/"

STATICFILES_DIRS = [
    os.path.join(BASE_DIR, "static"),
]

STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles")

# WhiteNoise configuration
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

# CORS Configuration
CORS_ALLOW_ALL_ORIGINS = True

cors_allowed_origins_env = os.environ.get("CORS_ALLOWED_ORIGINS", "")
cors_allowed_origin_env = os.environ.get("CORS_ALLOWED_ORIGIN", "")
origins_list = []
if cors_allowed_origins_env:
    origins_list += [
        origin.strip()
        for origin in cors_allowed_origins_env.split(",")
        if origin.strip()
    ]
if cors_allowed_origin_env:
    # Accept comma-separated even if the var name is singular
    origins_list += [
        origin.strip()
        for origin in cors_allowed_origin_env.split(",")
        if origin.strip()
    ]
CORS_ALLOWED_ORIGINS = origins_list

# Add localhost origins for development - always include when running locally
dev_origins = [
    "http://localhost:3000",
    "http://localhost:4200",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:4200",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "http://localhost:5173",  # Vite default
    "http://127.0.0.1:5173",
]

# Include dev origins if DEBUG is true OR if no explicit origins are configured
if DEBUG or not origins_list:
    CORS_ALLOWED_ORIGINS.extend(dev_origins)

# De-duplicate any origins added above
CORS_ALLOWED_ORIGINS = list(dict.fromkeys(CORS_ALLOWED_ORIGINS))

# Log CORS origins for debugging
if DEBUG:
    print(f"CORS_ALLOWED_ORIGINS: {CORS_ALLOWED_ORIGINS}")

# Test database configuration
if TESTING and os.environ.get("TEST_DB_NAME"):
    DATABASES["default"]["TEST"] = {"NAME": os.environ["TEST_DB_NAME"]}

# Optional: allow matching origins by regex (comma-separated)
CORS_ALLOWED_ORIGIN_REGEXES_ENV = os.environ.get("CORS_ALLOWED_ORIGIN_REGEXES", "")
if CORS_ALLOWED_ORIGIN_REGEXES_ENV:
    CORS_ALLOWED_ORIGIN_REGEXES = [
        rgx.strip() for rgx in CORS_ALLOWED_ORIGIN_REGEXES_ENV.split(",") if rgx.strip()
    ]
else:
    CORS_ALLOWED_ORIGIN_REGEXES = []

# Whether to allow credentials (cookies/Authorization with CORS)
CORS_ALLOW_CREDENTIALS = os.environ.get("CORS_ALLOW_CREDENTIALS", "True").lower() in (
    "true",
    "1",
    "yes",
)

CORS_ALLOW_HEADERS = list(default_headers) + [
    "x-user-id",
    "authorization",
    "content-type",
    "accept",
    "origin",
    "x-requested-with",
    "x-openai-api-key",  # For API key header
]
CORS_ALLOW_METHODS = list(default_methods) + [
    "OPTIONS",  # Ensure OPTIONS is explicitly allowed
]

# Ensure preflight requests are handled properly
CORS_PREFLIGHT_MAX_AGE = 86400  # 24 hours

# Security headers (production only)
if not DEBUG and not TESTING:
    SECURE_SSL_REDIRECT = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_SAMESITE = "Lax"
    CSRF_COOKIE_SAMESITE = "Lax"
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    X_FRAME_OPTIONS = "DENY"
    SECURE_CONTENT_TYPE_NOSNIFF = True
    REFERRER_POLICY = "same-origin"
else:
    SECURE_SSL_REDIRECT = False
    if TESTING:
        SESSION_COOKIE_SECURE = False
        CSRF_COOKIE_SECURE = False

# SimpleJWT Configuration
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(
        minutes=int(os.environ.get("JWT_ACCESS_TOKEN_LIFETIME_MINUTES", "60"))
    ),
    "REFRESH_TOKEN_LIFETIME": timedelta(
        days=int(os.environ.get("JWT_REFRESH_TOKEN_LIFETIME_DAYS", "30"))
    ),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": False,
    "LEEWAY": int(os.environ.get("JWT_LEEWAY_SECONDS", "60")),
}

# Feature flags
ALLOW_BOOTSTRAP_SUPERUSER = (
    os.environ.get("ALLOW_BOOTSTRAP_SUPERUSER", "False") == "True"
)
BOOTSTRAP_TOKEN = os.environ.get("BOOTSTRAP_TOKEN", "")
SCRAPING_ENABLED = os.environ.get("SCRAPING_ENABLED", "False") == "True"
CADDY_AGENT_URL = os.environ.get("CADDY_AGENT_URL", "http://localhost:3011")
USE_CADDY_AGENT_EXTRACTION = os.environ.get("USE_CADDY_AGENT_EXTRACTION", "").lower() in ("1", "true", "yes")
SCREENSHOT_DIR = os.environ.get("SCREENSHOT_DIR", "/app/screenshots")

# Logging configuration
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {process:d} {thread:d} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "WARNING",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "django.server": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "job_hunting": {
            "handlers": ["console"],
            "level": "DEBUG" if DEBUG else "INFO",
            "propagate": False,
        },
    },
}

# Wire logfire after LOGGING so our LogfireLoggingHandler piggy-backs on
# the already-installed dictConfig. Silent no-op when LOGFIRE_TOKEN is
# unset. Also instruments Django request spans + httpx / openai /
# anthropic calls made inside views.
from job_hunting.logfire_setup import setup_logfire  # noqa: E402

setup_logfire("career_caddy_api")

# Resume export template path
RESUME_EXPORT_TEMPLATE = os.path.join(BASE_DIR, "templates", "resume_export.docx")

# Email configuration
EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend"
)
EMAIL_HOST = os.environ.get("EMAIL_HOST", "localhost")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "True").lower() in (
    "true",
    "1",
    "yes",
)
EMAIL_USE_SSL = os.environ.get("EMAIL_USE_SSL", "False").lower() in (
    "true",
    "1",
    "yes",
)
DEFAULT_FROM_EMAIL = os.environ.get(
    "DEFAULT_FROM_EMAIL", "noreply@careercaddy.online"
)
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:4200")

# Stable identifier for this Career Caddy instance, used as JobPost
# `source_instance` default and as the host portion of the ActivityPub
# Actor / object IDs the /as-object/ adapter emits. Local installs
# default to "localhost"; prod is set via the CAREER_CADDY_INSTANCE env
# var (careercaddy.online). Federated rows are detected by comparing
# JobPost.source_instance against this value.
CAREER_CADDY_INSTANCE = os.environ.get("CAREER_CADDY_INSTANCE", "localhost")

# Phase C dedupe redesign — repost threshold (days). When two
# JobPosts share a ``normalized_fingerprint`` and the candidate is
# this many days older than ``now()``, ``compute_duplicate_candidates``
# emits the ``"repost"`` reason code instead of
# ``"normalized_fingerprint"``. The intuition: a slug-fold match
# inside the active hiring window is almost always the same role
# being re-listed for cross-platform dedupe; a slug-fold match across
# a multi-week gap is the company posting the same role in a NEW
# hiring cycle (same role, different cycle — operator should keep
# both rows linked but independently queryable).
#
# 14 days is the default tuning point — short enough to bias toward
# "same cycle" for normal cross-posting noise, long enough that the
# typical 2-3 week re-listing rhythm crosses it. Per-deployment
# tunable via the env var of the same name.
DEDUPE_REPOST_THRESHOLD_DAYS = int(
    os.environ.get("DEDUPE_REPOST_THRESHOLD_DAYS", "14")
)

# INSTANCE_ORIGIN — full origin (scheme + host[+port]) used to mint
# every ActivityPub URI this instance emits: actor IDs, object IDs,
# WebFinger ``links.href``, future Outbox / Inbox URIs. Splitting this
# from ``CAREER_CADDY_INSTANCE`` lets local-dev / Mastodon-peer harness
# (Plans/ActivityPub Phase 5 — federation proper/Local test harness)
# drive http://localhost:8000 or http://api:8000 without forcing the
# JobPost.source_instance default to change shape. When unset, code
# paths fall back to ``https://{CAREER_CADDY_INSTANCE}`` for
# backwards-compatibility with the Phase 4 ``as_object`` adapter.
INSTANCE_ORIGIN = os.environ.get("INSTANCE_ORIGIN", "http://localhost:8000")

# Page size for the Phase 5b Actor Outbox enumeration. Mastodon's UI
# fetches /outbox?page=1 once the actor is discovered and uses the
# OrderedCollection's `first` / `last` URIs to paginate. 20 is the
# upper-mid of typical AP peer defaults (Mastodon 20, Lemmy 20-50);
# keep tunable in case a peer chokes on larger pages once 5d dispatch
# starts populating real history.
ACTIVITYPUB_OUTBOX_PAGE_SIZE = 20

# BACK-102 — instance publish-UI capability (self-host seam). Governs
# whether the frontend renders the per-post publish-to-fediverse button:
#   off           — never show it (the public-safe baseline for self-hosters)
#   operator_only — show it to staff only (the frontend gates on is_staff)
#   all_users     — show it to every authenticated user
# Surfaced read-only on /api/v1/healthcheck/ as `federation_publish_ui` so
# the SPA can gate the button without a code change. Doug's instance runs
# `operator_only` ("someone else who spins this up may want publish ... but
# my users do not"); a self-hoster overrides via the env var. Invalid
# values fall back to the safe `off`.
_FEDERATION_PUBLISH_UI_CHOICES = ("off", "operator_only", "all_users")
FEDERATION_PUBLISH_UI = os.environ.get(
    "FEDERATION_PUBLISH_UI", "operator_only"
)
if FEDERATION_PUBLISH_UI not in _FEDERATION_PUBLISH_UI_CHOICES:
    FEDERATION_PUBLISH_UI = "off"

# ---------------------------------------------------------------------------
# ActivityPub Phase 5c — inbox + Follow + HTTP Signatures.
#
# Five operator-tunables governing peer key caching, replay protection
# window, per-instance rate limiting, outbound delivery timeout, and
# request body size cap. All env-overridable so a self-hoster can
# tighten/loosen without code changes.
#
# Rationale:
# - Key cache 5min: peers rotate keys rarely; 5min keeps us responsive
#   to rotation without re-fetching every inbound POST.
# - Date window 5min: matches Mastodon's default tolerance for clock
#   skew. Tighter rejects legit traffic from drifty NTP-less peers.
# - Rate limit 1000/hour per instance host: well above legitimate
#   federation rate (Mastodon's busiest peers send ~50/hour to one
#   actor). Per-instance instead of per-IP so a multi-host federated
#   server doesn't trip on its own fan-out, and a single instance
#   can't grief the limit for everyone else.
# - 10s outbound timeout: typical Mastodon inbox p99 is <2s; 10s
#   absorbs slow peers without holding the request-handler hostage.
# - 1MB body cap: largest legitimate Mastodon activity (long status
#   with multiple attachment refs) is ~50KB; 1MB is generous defence
#   against memory-exhaustion via giant POSTs.
ACTIVITYPUB_PEER_KEY_CACHE_TTL = int(
    os.environ.get("ACTIVITYPUB_PEER_KEY_CACHE_TTL", "300")
)
ACTIVITYPUB_DATE_WINDOW_SECONDS = int(
    os.environ.get("ACTIVITYPUB_DATE_WINDOW_SECONDS", "300")
)
ACTIVITYPUB_INBOX_RATE_LIMIT_PER_HOUR = int(
    os.environ.get("ACTIVITYPUB_INBOX_RATE_LIMIT_PER_HOUR", "1000")
)
ACTIVITYPUB_OUTBOUND_DELIVERY_TIMEOUT = int(
    os.environ.get("ACTIVITYPUB_OUTBOUND_DELIVERY_TIMEOUT", "10")
)
ACTIVITYPUB_BODY_MAX_BYTES = int(
    os.environ.get("ACTIVITYPUB_BODY_MAX_BYTES", str(1_048_576))
)

# ---------------------------------------------------------------------------
# CC-127 — inbox accept-then-async + bounded/negatively-cached key fetch.
#
# The inbound inbox verifies HTTP Signatures by fetching the remote
# sender's public key over HTTP. That fetch used to run synchronously on
# the web-worker thread with the 10s ACTIVITYPUB_OUTBOUND_DELIVERY_TIMEOUT
# and the requests default of 30 redirects — a slow / dead / redirect-
# looping peer could pin a worker for ~40s, and ~45% of deliveries (self-
# Delete broadcasts from suspended accounts, dead peers, scanners) 401'd,
# each 401 triggering an uncached-refetch redelivery storm.
#
# Fix: the inbox now returns 202 immediately and defers verify+process to
# the django-q worker (ACTIVITYPUB_INBOX_DISPATCH_SYNC below). The key
# fetch itself is bounded and negatively cached so even the worker can't
# hang and the storm stops re-paying the network cost.
#
# - split connect/read timeout (3s connect / 10s read): a DEAD host hangs
#   in the connect phase, so a tight 3s connect fails the storm sources
#   fast; a 10s read (matches Mastodon's app/lib/request.rb read_timeout)
#   still lets a slow-but-ALIVE legit peer respond. Since the fetch now
#   runs in the qcluster worker (not the web thread) we don't need the
#   brutal 2-3s total Doug floated — that would also drop live-but-slow
#   peers, which accept-then-async can't recover (the peer got a 202 and
#   won't redeliver). Both bounds are env-tunable.
# - redirect cap 3 (matches Mastodon max_hops=3): bare requests allows 30,
#   letting a redirect loop multiply the per-hop timeout into the observed
#   ~40s hangs.
# - negative-cache TTL 300s: a failed key fetch (unreachable / 404 deleted
#   actor / no publicKey) is remembered so an ActivityPub redelivery storm
#   short-circuits without network I/O. Mirrors Mastodon's Stoplight
#   circuit-breaker on the key refresh. Kept to 300s (== positive-cache
#   TTL) to bound how long a transiently-down-but-legit peer is dropped.
ACTIVITYPUB_PEER_KEY_FETCH_CONNECT_TIMEOUT = float(
    os.environ.get("ACTIVITYPUB_PEER_KEY_FETCH_CONNECT_TIMEOUT", "3")
)
ACTIVITYPUB_PEER_KEY_FETCH_READ_TIMEOUT = float(
    os.environ.get("ACTIVITYPUB_PEER_KEY_FETCH_READ_TIMEOUT", "10")
)
ACTIVITYPUB_PEER_KEY_FETCH_MAX_REDIRECTS = int(
    os.environ.get("ACTIVITYPUB_PEER_KEY_FETCH_MAX_REDIRECTS", "3")
)
ACTIVITYPUB_PEER_KEY_NEG_CACHE_TTL = int(
    os.environ.get("ACTIVITYPUB_PEER_KEY_NEG_CACHE_TTL", "300")
)

# When False (prod default), the inbox edge enqueues verify+process to the
# django-q worker (job_hunting.lib.federation_inbox.process_inbound_activity)
# and returns 202 without touching the network on the web thread. When True
# it runs verify+process in-band at the enqueue call site. Defaulted True
# under TESTING (below) so the inbox test-suite observes side effects
# synchronously; the dedicated async tests flip it False + assert the
# async_task enqueue. Distinct from Q_CLUSTER['sync'] (which is NOT toggled
# globally under TESTING — see the Q_CLUSTER note) so this cutover doesn't
# force every other async_task call site in-band.
ACTIVITYPUB_INBOX_DISPATCH_SYNC = os.environ.get(
    "ACTIVITYPUB_INBOX_DISPATCH_SYNC", "False"
).lower() in ("true", "1", "yes")

# ---------------------------------------------------------------------------
# ActivityPub Phase 5d — outbound dispatch worker.
#
# The django-q2 worker (already up — Phase 1 of the queue rollout) runs
# ``dispatch_one`` tasks scheduled by ``enqueue_jobpost_activity`` whenever
# a public JobPost is created / updated / deleted. Tunables:
#
# - retry backoff: [1m, 5m, 30m, 4h, 24h] → 6th attempt dead-letters.
#   Mirrors Mastodon's exponential schedule; long enough at the tail to
#   absorb a peer instance being down for a day without spamming.
# - dead-letter at attempt 6: covers the 5 backoff entries above plus a
#   final shot per the schedule's index math.
# - outbound POST timeout 10s: shared with 5c inbound delivery (matches
#   ACTIVITYPUB_OUTBOUND_DELIVERY_TIMEOUT but kept separate so a slow-
#   inbox peer can still receive WebFinger-class requests under tighter
#   budget if we ever split it).
# - operator kill-switch: setting ACTIVITYPUB_FEDERATION_ENABLED=False
#   short-circuits ``enqueue_jobpost_activity`` so signals stop fanning
#   out (worker tasks already in flight still drain).
ACTIVITYPUB_DISPATCH_RETRY_BACKOFF_SECONDS = [60, 300, 1800, 14400, 86400]
ACTIVITYPUB_DISPATCH_DEAD_LETTER_AT_RETRY = 6
ACTIVITYPUB_DISPATCH_TIMEOUT_SECONDS = int(
    os.environ.get("ACTIVITYPUB_DISPATCH_TIMEOUT_SECONDS", "10")
)
ACTIVITYPUB_FEDERATION_ENABLED = os.environ.get(
    "ACTIVITYPUB_FEDERATION_ENABLED", "True"
).lower() in ("true", "1", "yes")

# ---------------------------------------------------------------------------
# ActivityPub Phase 5e — federated JobPost ingestion.
#
# Inbound Create(Note) activities verified by the 5c inbox handler are
# turned into local JobPost rows via job_hunting.lib.federation_ingest.
# Tunables:
#
# - body cap (256KB): defensive ceiling on AS2 Note.content size so a
#   misbehaving / malicious peer can't fill our description column with
#   a megabyte of HTML. Activities that overshoot are logged + rejected.
# - per-instance quota (100/hour of NEW rows): bounds how fast any single
#   peer can grow our JobPost table. Counts only `created`, NOT `merged`
#   — a flood of dedup hits from a legitimate refanout doesn't lock the
#   peer out. Tracked in Django cache keyed on
#   ``ap:ingest_quota:<instance_host>:<hour_int>``.
# - operator kill-switch: ACTIVITYPUB_INGEST_ENABLED=False short-circuits
#   the ingest call inside the inbox handler. The activity is still
#   logged to FederationActivity (5c contract) so once the operator
#   re-enables ingestion, 5e's replay walk can pick the activities up.
ACTIVITYPUB_INGEST_BODY_MAX_BYTES = int(
    os.environ.get("ACTIVITYPUB_INGEST_BODY_MAX_BYTES", str(262_144))
)
ACTIVITYPUB_INGEST_INSTANCE_QUOTA_PER_HOUR = int(
    os.environ.get("ACTIVITYPUB_INGEST_INSTANCE_QUOTA_PER_HOUR", "100")
)
ACTIVITYPUB_INGEST_ENABLED = os.environ.get(
    "ACTIVITYPUB_INGEST_ENABLED", "True"
).lower() in ("true", "1", "yes")

# CC-68 — inbound Note→JobPost ingest is DISABLED by default (premature).
#
# Distinct from ACTIVITYPUB_INGEST_ENABLED (the operator kill-switch that
# defaults ON). This flag governs whether a verified inbound Create(Note)
# is allowed to MINT a local JobPost at all. It defaults OFF because
# inbound ingest has no subscription gate yet: the only filters today are
# object-type in {Note, Article}, AS2 Public audience, and canonical_link
# dedup — none of which answer "is this Note actually a job posting?" or
# "did the operator opt in to this sender?". Result: a plain fediverse
# toot/mention delivered to a local actor's inbox became a JobPost with
# no Company → rendered "missing" in the UI.
#
# When OFF: the inbox handler still writes the FederationActivity audit
# row (so we keep a trace of what arrived and a future re-enable can
# replay), but federation_ingest.ingest_create_note short-circuits to a
# SKIPPED outcome BEFORE creating any JobPost or JobPostDiscovery row.
# See the "Re-enable design" block in lib/federation_ingest.py for the
# gates a proper re-enable must add (positive CC marker + per-actor
# subscription opt-in). Set FEDERATION_INBOUND_INGEST_ENABLED=True to
# restore the legacy create-on-inbound behavior.
FEDERATION_INBOUND_INGEST_ENABLED = os.environ.get(
    "FEDERATION_INBOUND_INGEST_ENABLED", "False"
).lower() in ("true", "1", "yes")

PASSWORD_RESET_TIMEOUT = int(os.environ.get("PASSWORD_RESET_TIMEOUT", "3600"))

# Registration control — set REGISTRATION_OPEN=true to allow public signups
REGISTRATION_OPEN = os.environ.get("REGISTRATION_OPEN", "false").lower() in ("true", "1", "yes")

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

if TESTING:
    EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    REGISTRATION_OPEN = True
    # Inbox verify+process runs in-band under TESTING so the existing
    # inbox suite observes side effects synchronously (the pre-CC-127
    # contract was synchronous). Dedicated async tests override this to
    # False and assert the async_task enqueue. Unlike Q_CLUSTER['sync'],
    # this only affects the inbox path — no other async_task call site.
    ACTIVITYPUB_INBOX_DISPATCH_SYNC = True

# ---------------------------------------------------------------------------
# Q_CLUSTER — django-q2 task queue configuration.
#
# Phase 1 of Plans/Job-queue integration — django-q2 phased rollout.
# The worker container runs `manage.py qcluster` against the existing
# Postgres DB (no new infrastructure). Tasks are enqueued from views
# via `async_task('module.path', *args)`; the qcluster process picks
# them up off the django_q_ormq queue table.
#
# Defaults rationale (open question [?] Worker count / timeout defaults
# in the plan node):
#   - 2 workers: matches the archived plan's recommendation; small
#     enough to fit comfortably alongside api on the rn host.
#   - 300s timeout: covers Score / Summary / Cover Letter / Resume /
#     Answer / Question tier-1 LLM calls. parse_scrape (Phase 5) will
#     override to 600s via the task's `timeout=` kwarg.
#   - 4s poll: standard django-q2 default; balance between worker
#     responsiveness and DB load.
#   - retry=0 globally: per-task retry policies are explicit at the
#     async_task() call site (federation dispatch will override).
#
# This config block has no behavior impact in Phase 1 — only the
# `health_check` task exists. Subsequent phases migrate the 9
# daemon-thread spawn points + 2 polling daemons.
Q_CLUSTER = {
    "name": "career_caddy",
    # Env-driven so each host can be tuned independently (rn at 1 for
    # breathing room, off-rn workers at 2). Default 2 preserves the
    # Phase 1 baseline.
    "workers": int(os.environ.get("Q_WORKERS", 2)),
    "recycle": 500,
    "timeout": 300,
    "retry": 360,  # Higher than timeout so timed-out tasks don't re-queue immediately
    "queue_limit": 50,
    "bulk": 10,
    "orm": "default",
    "poll": 4,
    "label": "Career Caddy Tasks",
    "save_limit": 1000,
    "ack_failures": True,
    "max_attempts": 1,  # No automatic retry; per-task override via async_task(retry=N)
}

# ``Q_CLUSTER['sync']`` is NOT toggled globally under TESTING. Doing so
# would force every async_task() call site (resume ingest, score
# pipeline, summary, cover-letter, ...) to execute the task body
# in-band on the calling thread, surfacing every task exception as a
# 500 inside whatever view enqueued it. Several pre-existing tests
# (e.g. test_ingest_endpoint_blob) rely on the enqueue path returning
# 202-pending without touching the task body. Phase 5d federation
# dispatch tests opt into sync mode per-class via
# ``override_settings(Q_CLUSTER={..., 'sync': True})``.
