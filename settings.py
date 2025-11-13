#!/usr/bin/env python3
import os
from urllib.parse import urlparse

# Try to import the project's base settings if present,
# then layer environment-driven overrides below.
try:
    from job_hunting.settings import *  # noqa
except Exception:
    # Minimal safe defaults if the base settings module isn't available
    SECRET_KEY = os.environ.get("SECRET_KEY", "insecure-secret-key")
    DEBUG = os.environ.get("DEBUG", "False").lower() in ("1", "true", "yes")

    INSTALLED_APPS = [
        "django.contrib.admin",
        "django.contrib.auth",
        "django.contrib.contenttypes",
        "django.contrib.sessions",
        "django.contrib.messages",
        "django.contrib.staticfiles",
        "rest_framework",
        "corsheaders",
        "job_hunting",
    ]

    MIDDLEWARE = [
        "corsheaders.middleware.CorsMiddleware",
        "django.middleware.security.SecurityMiddleware",
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.middleware.common.CommonMiddleware",
        "django.middleware.csrf.CsrfViewMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "django.contrib.messages.middleware.MessageMiddleware",
        "django.middleware.clickjacking.XFrameOptionsMiddleware",
        "job_hunting.middleware.sqlalchemy_session.SQLAlchemySessionMiddleware",
    ]

    ROOT_URLCONF = "job_hunting.urls"

    TEMPLATES = [
        {
            "BACKEND": "django.template.backends.django.DjangoTemplates",
            "DIRS": [os.path.join(os.path.dirname(__file__), "templates")],
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

    WSGI_APPLICATION = "job_hunting.wsgi.application"
    ASGI_APPLICATION = "job_hunting.asgi.application"

    LANGUAGE_CODE = "en-us"
    TIME_ZONE = "UTC"
    USE_I18N = True
    USE_TZ = True
    STATIC_URL = "static/"

# Merge/extend ALLOWED_HOSTS, CORS, and CSRF from env with sensible defaults
ALLOWED_HOSTS = list(set(
    (globals().get("ALLOWED_HOSTS") or []) +
    [h for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h] +
    ["api.careercaddy.online"]
))

CORS_ALLOWED_ORIGINS = list(set(
    (globals().get("CORS_ALLOWED_ORIGINS") or []) +
    [o.strip() for o in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()] +
    ([os.environ.get("CORS_ALLOWED_ORIGIN").strip()] if os.environ.get("CORS_ALLOWED_ORIGIN") else []) +
    ["https://careercaddy.online"]
))

# Add development origins for CORS
if os.environ.get("DEBUG", "False").lower() in ("1", "true", "yes"):
    CORS_ALLOWED_ORIGINS.extend([
        "http://localhost:3000",
        "http://localhost:4200", 
        "http://127.0.0.1:3000",
        "http://127.0.0.1:4200",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://localhost:5173",  # Vite default
        "http://127.0.0.1:5173",
    ])

# De-duplicate CORS origins
CORS_ALLOWED_ORIGINS = list(dict.fromkeys(CORS_ALLOWED_ORIGINS))

CSRF_TRUSTED_ORIGINS = list(set(
    (globals().get("CSRF_TRUSTED_ORIGINS") or []) +
    [o for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if o] +
    ["https://careercaddy.online", "https://api.careercaddy.online"]
))

def _build_db_from_env():
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        parsed = urlparse(db_url)
        if parsed.scheme in ("postgres", "postgresql"):
            return {
                "default": {
                    "ENGINE": "django.db.backends.postgresql",
                    "NAME": parsed.path.lstrip("/") or os.environ.get("POSTGRES_DB", "job_hunting"),
                    "USER": parsed.username or os.environ.get("POSTGRES_USER", "postgres"),
                    "PASSWORD": parsed.password or os.environ.get("POSTGRES_PASSWORD", ""),
                    "HOST": parsed.hostname or os.environ.get("POSTGRES_HOST", "db"),
                    "PORT": str(parsed.port or os.environ.get("POSTGRES_PORT", "5432")),
                }
            }
    if os.environ.get("POSTGRES_HOST") or os.environ.get("POSTGRES_DB"):
        return {
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": os.environ.get("POSTGRES_DB", "job_hunting"),
                "USER": os.environ.get("POSTGRES_USER", "postgres"),
                "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
                "HOST": os.environ.get("POSTGRES_HOST", "db"),
                "PORT": os.environ.get("POSTGRES_PORT", "5432"),
            }
        }
    return None

_env_db = _build_db_from_env()
if _env_db:
    DATABASES = _env_db
elif "DATABASES" not in globals():
    # Fallback to sqlite so the app can start in dev if Postgres isn't configured.
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.path.join(BASE_DIR, "db.sqlite3"),
        }
    }

# Allow overriding DEBUG from env even if base settings defined it
if "DEBUG" in os.environ:
    DEBUG = os.environ.get("DEBUG", "False").lower() in ("1", "true", "yes")
