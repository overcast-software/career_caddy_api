"""Tests for the Cloud Run qcluster health-port shim (CC-190/CC-199).

The shim (scripts/qcluster_web.py) backgrounds `manage.py qcluster` and serves
a health port so Cloud Run's startup/liveness probe (GET /healthz) can prove the
drainer is up. The load-bearing invariant: it must return 200 ONLY while the
qcluster child is alive, and exit non-zero when the child dies — so a dead
drainer never sits behind a green revision with OrmQ rows piling up.

These tests exercise the supervisor with synthetic child commands (a resident
sleeper, an instant exiter) rather than a real qcluster — the supervision
contract is what matters, and it's Django-free stdlib code.
"""

import socket
import sys
import threading
import time
import urllib.error
import urllib.request

from django.test import SimpleTestCase

# scripts/ is not a package; load the module by path.
import importlib.util
from pathlib import Path

_SHIM = Path(__file__).resolve().parents[2] / "scripts" / "qcluster_web.py"
_spec = importlib.util.spec_from_file_location("qcluster_web", _SHIM)
qcluster_web = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qcluster_web)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get(port: int, path: str = "/healthz", timeout: float = 2.0):
    url = f"http://127.0.0.1:{port}{path}"
    try:
        # Fixed http://127.0.0.1 test URL — no untrusted scheme/host.
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # nosec B310
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class QclusterWebShimTests(SimpleTestCase):
    def _run_supervisor(self, cmd, port):
        sup = qcluster_web._Supervisor(cmd)
        sup._port = port  # not used by serve(); PORT is module-level
        rc_box = {}

        def _serve():
            # Patch the module-level PORT/HOST so serve() binds our test port.
            qcluster_web.PORT = port
            qcluster_web.HOST = "127.0.0.1"
            rc_box["rc"] = sup.serve()

        t = threading.Thread(target=_serve, daemon=True)
        t.start()
        return sup, t, rc_box

    def _wait_for_server(self, port, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                _get(port, timeout=0.5)
                return
            except urllib.error.URLError:
                time.sleep(0.05)
        self.fail("health server never came up")

    def test_healthy_while_child_alive(self):
        """Probe returns 200 while the (resident) child is running."""
        port = _free_port()
        # A child that stays alive long enough to probe.
        cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
        sup, t, rc_box = self._run_supervisor(cmd, port)
        try:
            self._wait_for_server(port)
            status, body = _get(port, "/healthz")
            self.assertEqual(status, 200)
            self.assertIn(b"ok", body)
            # Any GET path is fine — the probe only needs a 200 from the process.
            status_root, _ = _get(port, "/")
            self.assertEqual(status_root, 200)
        finally:
            if sup.proc and sup.proc.poll() is None:
                sup.proc.terminate()
            t.join(timeout=10)

    def test_exits_nonzero_when_child_dies(self):
        """When the child exits non-zero, the server tears down and serve()
        returns that non-zero code — Cloud Run then restarts the revision."""
        port = _free_port()
        # Child exits with code 7 after a beat.
        cmd = [sys.executable, "-c", "import sys, time; time.sleep(0.3); sys.exit(7)"]
        sup, t, rc_box = self._run_supervisor(cmd, port)
        self._wait_for_server(port)
        t.join(timeout=10)
        self.assertFalse(t.is_alive(), "supervisor did not exit after child died")
        self.assertEqual(rc_box.get("rc"), 7)

    def test_exits_nonzero_on_clean_child_exit_is_zero(self):
        """A child that exits 0 propagates rc 0 (defensive: qcluster normally
        never exits 0, but we mirror the child faithfully)."""
        port = _free_port()
        cmd = [sys.executable, "-c", "import time; time.sleep(0.3)"]
        sup, t, rc_box = self._run_supervisor(cmd, port)
        self._wait_for_server(port)
        t.join(timeout=10)
        self.assertEqual(rc_box.get("rc"), 0)
