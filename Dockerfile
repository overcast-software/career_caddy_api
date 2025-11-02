# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System deps for psycopg2 and health checks
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first (better layer caching)
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip wheel && \
    pip install -r requirements.txt

# Copy project
COPY . /app

# Set environment for Django management commands
ENV DJANGO_SETTINGS_MODULE=job_hunting.settings \
    SECRET_KEY=build-time-dummy-secret \
    DEBUG=False

# Collect static (ignore if not configured)
RUN python manage.py collectstatic --noinput || true

# Create non-root user and data directory
RUN useradd -m appuser && \
    mkdir -p /data && \
    chown -R appuser:appuser /data

# Copy configuration files
COPY gunicorn.conf.py /app/gunicorn.conf.py
COPY scripts/entrypoint.sh /app/scripts/entrypoint.sh
RUN chmod +x /app/scripts/entrypoint.sh

USER appuser

EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/v1/healthcheck || exit 1

# Use entrypoint script
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
