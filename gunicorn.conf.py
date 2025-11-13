import multiprocessing
import os

# Make bind address configurable via environment
host = os.getenv("GUNICORN_HOST", "127.0.0.1")
port = os.getenv("GUNICORN_PORT", os.getenv("PORT", "8000"))
bind = f"{host}:{port}"
workers = multiprocessing.cpu_count() * 2 + 1
timeout = int(os.getenv("GUNICORN_TIMEOUT", "600"))
graceful_timeout = int(os.getenv("GUNICORN_GRACEFUL_TIMEOUT", "90"))
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = "info"
