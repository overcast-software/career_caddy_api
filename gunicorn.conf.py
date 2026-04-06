import multiprocessing
import os

# Make bind address configurable via environment
host = os.getenv("GUNICORN_HOST", "127.0.0.1")
port = os.getenv("GUNICORN_PORT", os.getenv("PORT", "8000"))
bind = f"{host}:{port}"
# Cap workers — the CPU*2+1 formula is too aggressive on small servers.
# Default to 3; override with GUNICORN_WORKERS env var.
workers = int(os.getenv("GUNICORN_WORKERS", min(multiprocessing.cpu_count() * 2 + 1, 3)))
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
graceful_timeout = int(os.getenv("GUNICORN_GRACEFUL_TIMEOUT", "30"))
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = "info"
