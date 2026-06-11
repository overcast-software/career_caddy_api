import multiprocessing
import os

# Make bind address configurable via environment
host = os.getenv("GUNICORN_HOST", "127.0.0.1")
port = os.getenv("GUNICORN_PORT", os.getenv("PORT", "8000"))
bind = f"{host}:{port}"
# Cap workers — the CPU*2+1 formula is too aggressive on small servers.
# Default to 3; override with GUNICORN_WORKERS env var.
workers = int(os.getenv("GUNICORN_WORKERS", min(multiprocessing.cpu_count() * 2 + 1, 3)))
# Threads per worker. Default 1 = sync worker (legacy behavior).
# Setting GUNICORN_THREADS > 1 flips worker_class to gthread, which
# gives each worker N threads sharing the same process. Net concurrent
# capacity = workers * threads. Memory cost is sub-linear in threads
# (shared imports + per-thread stack), so threads is the cheaper lever
# for I/O-bound Django request workloads.
threads = int(os.getenv("GUNICORN_THREADS", "1"))
if threads > 1:
    worker_class = "gthread"
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
graceful_timeout = int(os.getenv("GUNICORN_GRACEFUL_TIMEOUT", "30"))
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = "info"
