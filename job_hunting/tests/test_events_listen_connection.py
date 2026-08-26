"""How the SSE LISTEN connection reaches Postgres (CC-252 regression).

SSE delivered nothing in production for at least seven days while every
stream returned 200 and kept sending keepalives. The dispatcher's LISTEN
connection was dialling ``localhost`` on Cloud Run and being refused, once
per second, for as long as anyone had a tab open.

The cause was that ``_open_listen_connection`` hand-built its psycopg2
connect out of ``settings.DATABASES["default"]`` and read the host as
``db.get("HOST") or "localhost"``. That reconstruction is only correct for
the config shapes where the host happens to live in ``HOST``. Production
does not use one of those:

    deploy/terraform/gcp/secrets.tf builds
        postgresql://postgres:<pw>@/job_hunting?host=/cloudsql/<instance>

    dj_database_url.parse() turns that into
        HOST    = ""                                 <- no hostname in the netloc
        OPTIONS = {"host": "/cloudsql/<instance>"}   <- the whole query string

Django's postgres backend splats ``OPTIONS`` into its connect kwargs and
only overrides ``host`` when ``HOST`` is truthy, so the ORM found the
socket and the LISTEN connection did not. The ``or "localhost"`` fallback
then turned "I don't understand this config" into "connected somewhere
else", which is the part that made it silent.

Every pre-existing SSE test either mocks ``_open_listen_connection`` or
runs against a config whose host *is* in ``HOST`` — so the whole suite was
green while prod was dead. These tests pin the CONNECTION rather than the
contract: they drive real ``DATABASE_URL`` strings through the same
``dj_database_url`` -> Django -> psycopg2 path production uses, and assert
on the kwargs that actually reach ``psycopg2.connect``.

The Cloud SQL case here fails against the pre-fix code (it resolved
``host="localhost"``).

The invariant all of this is protecting is symmetrical: **this connection
goes wherever Django goes.** So the shapes below include one with no host
at all, which Django connects with by letting libpq apply its own default
— and which therefore must not be rejected here either. Inventing a host
and refusing a host Django accepts are the same bug pointed in opposite
directions, and only the first one has happened so far.
"""
from __future__ import annotations

import contextlib
from unittest import mock

import dj_database_url
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db.utils import ConnectionHandler
from django.test import SimpleTestCase

from job_hunting.api import events
from job_hunting.lib import events as events_lib


CLOUD_SQL_SOCKET = "/cloudsql/cc-gcp-lab-dkh:us-west1:career-caddy-pg-us-west1"

# The URLs are named rather than indexed out of CONFIG_SHAPES so that
# adding a shape can't silently repoint a test at a different one.
COMPOSE_URL = "postgres://postgres:postgres@db:5432/job_hunting"
LOCALHOST_URL = "postgresql://postgres:postgres@localhost:5432/job_hunting"
CLOUD_SQL_URL = f"postgresql://postgres:s3cret@/job_hunting?host={CLOUD_SQL_SOCKET}"

# No host by any route: libpq falls back to its own default local socket.
# Django connects fine with this, so this function must too — rejecting it
# would recreate CC-252 from the other side, a working api beside a
# silently dead events service.
BARE_SOCKET_URL = "postgresql://postgres:pw@/job_hunting"

# No database name by any route: unusable however you connect. Django's
# own get_connection_params raises on it.
NO_DBNAME_URL = "postgresql://postgres:pw@db:5432/"

# (label, DATABASE_URL, expected libpq connect kwargs). These are the real
# shapes this project deploys: docker compose, the settings.py DEBUG
# fallback, and the GCP secret built in deploy/terraform/gcp/secrets.tf —
# plus the bare-socket shape a self-hoster could reasonably use.
CONFIG_SHAPES = [
    (
        "docker compose TCP host",
        COMPOSE_URL,
        {
            "dbname": "job_hunting",
            "user": "postgres",
            "password": "postgres",
            "host": "db",
            "port": 5432,
        },
    ),
    (
        "local development on localhost",
        LOCALHOST_URL,
        {
            "dbname": "job_hunting",
            "user": "postgres",
            "password": "postgres",
            "host": "localhost",
            "port": 5432,
        },
    ),
    (
        "Cloud SQL unix socket",
        CLOUD_SQL_URL,
        {
            "dbname": "job_hunting",
            "user": "postgres",
            "password": "s3cret",
            "host": CLOUD_SQL_SOCKET,
        },
    ),
    (
        "libpq default local socket",
        BARE_SOCKET_URL,
        {
            "dbname": "job_hunting",
            "user": "postgres",
            "password": "pw",
            "host": None,  # asserted as ABSENT, not as a value
        },
    ),
]


def _connections_for(url: str) -> ConnectionHandler:
    """A throwaway connection handler configured from ``url``.

    Built the way settings.py builds the real one, so the test exercises
    ``dj_database_url.parse`` rather than a hand-written settings dict.
    Nothing here opens a connection — ``get_connection_params()`` is pure.
    """
    return ConnectionHandler({"default": dj_database_url.parse(url)})


@contextlib.contextmanager
def _database_config(url: str):
    """Put ``url`` in place as THE database config, by every route.

    Both ``settings.DATABASES["default"]`` (what the pre-fix code read)
    and ``events.connections`` (what the fix reads) are pointed at the
    same parsed URL, so tests written against this helper assert on the
    CONFIG rather than on either implementation — and therefore fail
    against the pre-fix code for the real reason (a resolved host of
    ``localhost``) instead of tripping over a renamed symbol.
    """
    parsed = dj_database_url.parse(url)
    handler = ConnectionHandler({"default": dict(parsed)})
    with mock.patch.object(events, "connections", handler, create=True):
        with mock.patch.dict(settings.DATABASES, {"default": parsed}):
            yield


class TestListenConnectionParams(SimpleTestCase):
    """``_listen_connection_params`` resolves the host Django resolves."""

    def test_every_config_shape_resolves_its_real_host(self):
        for label, url, expected in CONFIG_SHAPES:
            with self.subTest(shape=label):
                with mock.patch.object(events, "connections", _connections_for(url)):
                    params = events._listen_connection_params()

                for key, value in expected.items():
                    if value is None:
                        # Django passes nothing for this parameter, so
                        # neither do we — libpq gets to apply its own
                        # default, exactly as it does for the ORM.
                        self.assertNotIn(key, params)
                    else:
                        self.assertEqual(params[key], value)

    def test_cloud_sql_socket_is_invisible_in_the_settings_host(self):
        """The mechanism, asserted rather than described.

        ``HOST`` is empty for the socket shape — so any implementation
        that reads it alone has nothing to go on, and a fallback is a
        guess. This is what the ``or "localhost"`` line was doing.
        """
        url = CLOUD_SQL_URL
        connections = _connections_for(url)
        settings_dict = connections["default"].settings_dict

        self.assertEqual(settings_dict["HOST"], "")
        self.assertEqual(settings_dict["OPTIONS"]["host"], CLOUD_SQL_SOCKET)

        with mock.patch.object(events, "connections", connections):
            params = events._listen_connection_params()
        self.assertEqual(params["host"], CLOUD_SQL_SOCKET)

    def test_no_host_is_passed_through_exactly_as_django_passes_it(self):
        """A URL Django connects with must not fail here.

        THE INVARIANT: this connection goes wherever Django goes. The
        old ``or "localhost"`` broke it by inventing a host; rejecting a
        no-host URL would break it from the other side, since Django
        accepts one — a self-hoster on that shape would get a working
        api beside a silently dead events service, which is CC-252 again
        with different inputs.

        Asserted against Django's own resolution rather than a literal,
        so the two cannot drift apart.
        """
        connections = _connections_for(BARE_SOCKET_URL)
        django_params = connections["default"].get_connection_params()
        self.assertNotIn("host", django_params)

        with mock.patch.object(events, "connections", connections):
            params = events._listen_connection_params()

        self.assertNotIn("host", params)
        self.assertEqual(params["dbname"], django_params["dbname"])

    def test_a_genuinely_unusable_config_still_fails_loudly(self):
        """Validation is Django's, and it still bites.

        Dropping our own host check does not mean anything goes: a URL
        with no database name is unusable by any route, and Django's
        ``get_connection_params`` raises before we reach psycopg2.
        """
        connections = _connections_for(NO_DBNAME_URL)

        with mock.patch.object(events, "connections", connections):
            with self.assertRaises(ImproperlyConfigured):
                events._listen_connection_params()

    def test_django_adapter_plumbing_is_not_passed_to_libpq(self):
        """Django adds a ``cursor_factory`` for the ORM's own cursor.

        It is not a libpq parameter and a raw LISTEN connection wants
        psycopg2's plain cursor, so it is stripped.
        """
        url = COMPOSE_URL
        with mock.patch.object(events, "connections", _connections_for(url)):
            params = events._listen_connection_params()

        self.assertNotIn("cursor_factory", params)
        self.assertNotIn("context", params)


class TestOpenListenConnection(SimpleTestCase):
    """The resolved params are what ``psycopg2.connect`` actually gets.

    These drive ``_open_listen_connection`` through ``_database_config``,
    which installs the URL by every route an implementation might read —
    so they are assertions about the CONFIG, and they fail against the
    pre-fix code with the real symptom rather than an import error.
    """

    def test_connect_receives_the_resolved_host_for_every_shape(self):
        for label, url, expected in CONFIG_SHAPES:
            with self.subTest(shape=label):
                with _database_config(url), mock.patch.object(
                    events.psycopg2, "connect"
                ) as connect:
                    events._open_listen_connection()

                kwargs = connect.call_args.kwargs
                if expected["host"] is None:
                    self.assertNotIn("host", kwargs)
                else:
                    self.assertEqual(kwargs["host"], expected["host"])
                self.assertEqual(kwargs["dbname"], expected["dbname"])
                self.assertEqual(kwargs["user"], expected["user"])
                self.assertEqual(kwargs["password"], expected["password"])

    def test_cloud_sql_never_resolves_to_localhost(self):
        """The exact prod failure, as an assertion.

        This is the test whose absence let CC-252 ship. Pre-fix it
        resolves ``host="localhost"``, which is what Cloud Run refused
        once a second for a week.
        """
        url = CLOUD_SQL_URL
        with _database_config(url), mock.patch.object(
            events.psycopg2, "connect"
        ) as connect:
            events._open_listen_connection()

        self.assertEqual(connect.call_args.kwargs["host"], CLOUD_SQL_SOCKET)

    def test_connection_is_autocommit_and_listening(self):
        """LISTEN needs a session-mode autocommit connection, and the
        connection is useless until it has issued the LISTEN."""
        url = COMPOSE_URL
        with _database_config(url), mock.patch.object(
            events.psycopg2, "connect"
        ) as connect:
            conn = events._open_listen_connection()

        conn.set_isolation_level.assert_called_once_with(
            events.psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT
        )
        cursor = connect.return_value.cursor.return_value.__enter__.return_value
        cursor.execute.assert_called_once_with(f"LISTEN {events_lib.CHANNEL};")

    def test_no_host_connects_and_lets_libpq_default(self):
        """The bare-socket shape connects; it does not raise.

        Pinned so the pass-through is deliberate rather than incidental.
        """
        with _database_config(BARE_SOCKET_URL), mock.patch.object(
            events.psycopg2, "connect"
        ) as connect:
            events._open_listen_connection()

        connect.assert_called_once()
        self.assertNotIn("host", connect.call_args.kwargs)
        self.assertEqual(connect.call_args.kwargs["dbname"], "job_hunting")
