"""Cloud Run health-port shim for the django-q2 ``qcluster`` drainer (CC-190/CC-199).

Cloud Run requires every container to answer a startup/liveness probe on
``$PORT``. ``manage.py qcluster`` is a portless pull-loop daemon — it opens no
socket — so a plain qcluster revision fails its probe forever. This supervisor
bridges that gap:

  1. spawn ``python manage.py qcluster`` as a child process,
  2. serve HTTP 200 on ``$PORT`` (probe path ``/healthz``) *while the child is
     alive*, 503 once it has exited,
  3. propagate the child's death — when qcluster exits, tear down the HTTP
     server and exit with a non-zero status so Cloud Run restarts the revision.

The load-bearing invariant is (3): we must NOT report healthy while the drainer
is dead. A healthy probe over a dead qcluster would leave OrmQ rows stranded
with the revision looking green — exactly the CC-199 failure mode.

Dependency-light on purpose: stdlib only (http.server, subprocess, threading,
signal). No Django import in the supervisor itself; Django only runs inside the
qcluster child.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The django-q2 drainer. sys.executable keeps us on the same interpreter/venv
# the container was launched with (the uv-managed /app/.venv).
QCLUSTER_CMD = [sys.executable, "manage.py", "qcluster"]

# Bind all interfaces: Cloud Run routes the startup/liveness probe to the
# container's external interface, so 0.0.0.0 is required (not a security gap —
# the worker service has no public invoker binding, see worker.tf).
HOST = os.environ.get("GUNICORN_HOST", "0.0.0.0")  # nosec B104
# Cloud Run injects $PORT (== the tf container_port, 8000). Fall back to 8000
# for local/dev parity with the api service.
PORT = int(os.environ.get("PORT", "8000"))


class _Supervisor:
    """Owns the qcluster child and the liveness flag the HTTP handler reads."""

    def __init__(self, cmd: list[str]) -> None:
        self.cmd = cmd
        self.proc: subprocess.Popen | None = None
        self.returncode: int | None = None
        self._httpd: ThreadingHTTPServer | None = None

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        # start_new_session=False: keep the child in our process group so a
        # Cloud Run SIGTERM to us can be forwarded deterministically.
        self.proc = subprocess.Popen(self.cmd)

    def wait_and_shutdown(self) -> None:
        """Block on the child; when it exits, stop the HTTP server.

        Runs in a daemon thread. Setting ``returncode`` before shutting the
        server down means any probe racing the exit sees 503, not 200.
        """
        assert self.proc is not None
        self.returncode = self.proc.wait()
        if self._httpd is not None:
            # shutdown() must be called from a different thread than serve_forever.
            self._httpd.shutdown()

    def forward_signal(self, signum: int, _frame) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signum)

    def serve(self) -> int:
        self.start()

        supervisor = self

        class Handler(BaseHTTPRequestHandler):
            def _respond(self) -> None:
                if supervisor.alive:
                    self.send_response(200)
                    body = b"ok\n"
                else:
                    # qcluster is gone — fail the probe so Cloud Run restarts us.
                    self.send_response(503)
                    body = b"qcluster down\n"
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 (http.server API)
                self._respond()

            def do_HEAD(self) -> None:  # noqa: N802 (http.server API)
                # Body-less variant for probes that use HEAD.
                if supervisor.alive:
                    self.send_response(200)
                else:
                    self.send_response(503)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()

            def log_message(self, *args) -> None:  # noqa: A002
                # Silence per-probe access logging (every 10s) — keep the log
                # readable for the qcluster child's own output.
                pass

        self._httpd = ThreadingHTTPServer((HOST, PORT), Handler)

        # signal.signal() only works on the main thread. In prod the shim IS the
        # main thread (qcluster-web.sh execs it as PID 1), so SIGTERM forwarding
        # is wired. In tests we drive serve() from a worker thread — skip the
        # handlers there rather than raising out of serve() (which would leave a
        # bound-but-unhandled socket).
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, self.forward_signal)
            signal.signal(signal.SIGINT, self.forward_signal)

        monitor = threading.Thread(target=self.wait_and_shutdown, daemon=True)
        monitor.start()

        try:
            self._httpd.serve_forever()
        finally:
            self._httpd.server_close()

        # serve_forever returned => the monitor called shutdown() because
        # qcluster exited (or a signal we forwarded terminated it). Make sure
        # the child is fully reaped and mirror its exit status.
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.returncode = self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.returncode = self.proc.wait()

        rc = self.returncode if self.returncode is not None else 1
        # Normalize signal-terminated children (negative rc) to non-zero so
        # Cloud Run treats it as a crash and restarts the revision.
        return rc if rc and rc > 0 else (0 if rc == 0 else 1)


def main() -> int:
    return _Supervisor(QCLUSTER_CMD).serve()


if __name__ == "__main__":
    sys.exit(main())
