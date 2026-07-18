#!/bin/bash
# Cloud Run entrypoint for the django-q2 qcluster worker (CC-190/CC-199).
#
# The worker Cloud Run service (deploy/terraform/gcp/worker.tf) invokes this
# script as its container `command`. It hands off to qcluster_web.py, which
# backgrounds `manage.py qcluster` and serves a health port on $PORT so the
# Cloud Run startup/liveness probe (GET /healthz) can prove the drainer is up.
#
# This worker does NOT migrate — the api service owns migrations
# (SA_SCHEMA_ON_POST_MIGRATE=False is set on the worker in worker.tf).
#
# exec so qcluster_web.py becomes the container's PID and receives SIGTERM
# directly on Cloud Run shutdown (it forwards the signal to the qcluster child).
set -euo pipefail

exec python /app/scripts/qcluster_web.py
