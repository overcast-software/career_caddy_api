import os
import sys
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
    CSRF_TRUSTED_ORIGINS.extend([
        "http://localhost:3000",
        "http://localhost:4200", 
        "http://127.0.0.1:3000",
        "http://127.0.0.1:4200",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
    ])

USE_TZ = True

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
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
    "rest_framework",
    "rest_framework.authtoken",
    "corsheaders",
    "job_hunting.apps.JobHuntingConfig",
]

ROOT_URLCONF = "job_hunting.urls"

# Database configuration with dj-database-url
import dj_database_url

DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL:
    DATABASES = {"default": dj_database_url.parse(DATABASE_URL)}
else:
    # Default to localhost Postgres in development
    if DEBUG:
        DATABASES = {"default": dj_database_url.parse("postgresql://postgres:postgres@localhost:5432/job_hunting")}
    else:
        # In production, DATABASE_URL must be set
        raise ImproperlyConfigured(
            "DATABASE_URL environment variable must be set in production. "
            "Example: postgresql://user:password@host:port/database"
        )

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
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
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": os.environ.get("DRF_ANON_THROTTLE_RATE", "100/day"),
        "user": os.environ.get("DRF_USER_THROTTLE_RATE", "1000/day"),
    },
}

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/3.2/howto/static-files/

STATIC_URL = "/static/"

# Only include static directory if it exists (prevents W004 warning in CI)
static_dir = os.path.join(BASE_DIR, "static")
if os.path.isdir(static_dir):
    STATICFILES_DIRS = [static_dir]
else:
    STATICFILES_DIRS = []

STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles")

# WhiteNoise configuration
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

# CORS Configuration
CORS_ALLOW_ALL_ORIGINS = False

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
if TESTING:
    # Disable persistent connections during tests to prevent "database is being accessed by other users"
    DATABASES["default"]["CONN_MAX_AGE"] = 0
    
    # Generate unique test database name to prevent collisions across parallel CI runs
    import re
    
    base_name = os.environ.get("TEST_DB_NAME", "job_hunting_ci")
    
    # Create unique suffix from CI metadata or process ID
    suffix_parts = []
    if os.environ.get("GITHUB_RUN_ID"):
        suffix_parts.append(os.environ["GITHUB_RUN_ID"])
    if os.environ.get("GITHUB_RUN_ATTEMPT"):
        suffix_parts.append(os.environ["GITHUB_RUN_ATTEMPT"])
    
    # Fallback to process ID if no CI metadata
    if not suffix_parts:
        suffix_parts.append(str(os.getpid()))
    
    suffix = "_".join(suffix_parts)
    unique_name = f"{base_name}_{suffix}"
    
    # Sanitize name to only alphanumeric and underscores, trim to 63 chars (Postgres limit)
    unique_name = re.sub(r'[^a-zA-Z0-9_]', '_', unique_name)[:63]
    
    DATABASES["default"]["TEST"] = {"NAME": unique_name}

# Optional: allow matching origins by regex (comma-separated)
CORS_ALLOWED_ORIGIN_REGEXES_ENV = os.environ.get("CORS_ALLOWED_ORIGIN_REGEXES", "")
if CORS_ALLOWED_ORIGIN_REGEXES_ENV:
    CORS_ALLOWED_ORIGIN_REGEXES = [
        rgx.strip() for rgx in CORS_ALLOWED_ORIGIN_REGEXES_ENV.split(",") if rgx.strip()
    ]
else:
    CORS_ALLOWED_ORIGIN_REGEXES = []

# Whether to allow credentials (cookies/Authorization with CORS)
CORS_ALLOW_CREDENTIALS = os.environ.get("CORS_ALLOW_CREDENTIALS", "True").lower() in ("true", "1", "yes")

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
from datetime import timedelta

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
    },
}

# Resume export template path
RESUME_EXPORT_TEMPLATE = os.path.join(BASE_DIR, "templates", "resume_export.docx")
