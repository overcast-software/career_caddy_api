# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System deps for psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
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

# Create non-root user
RUN useradd -m appuser
USER appuser

EXPOSE 8000

# Gunicorn entrypoint
CMD ["gunicorn", "job_hunting.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "60"]
