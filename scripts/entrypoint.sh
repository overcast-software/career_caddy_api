#!/bin/bash
set -euo pipefail

echo "Running database migrations..."
python manage.py migrate --noinput

echo "Starting gunicorn..."
exec gunicorn job_hunting.wsgi:application -c /app/gunicorn.conf.py
