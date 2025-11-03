import os
import sys
from django.core.management.base import BaseCommand, CommandError

# Support either psycopg (v3) or psycopg2 (v2)
_psycopg3 = None
_psycopg2 = None
try:
    import psycopg as _psycopg3  # type: ignore
except Exception:
    _psycopg3 = None

if _psycopg3 is None:
    try:
        import psycopg2 as _psycopg2  # type: ignore
        from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT  # type: ignore
    except Exception:
        _psycopg2 = None


class Command(BaseCommand):
    help = "Ensure the target Postgres database exists; create it if missing, then exit."

    def add_arguments(self, parser):
        parser.add_argument(
            "--maintenance-db",
            default=os.environ.get("POSTGRES_MAINTENANCE_DB", "postgres"),
            help="Database to connect to for administrative actions (default: %(default)s)",
        )

    def handle(self, *args, **options):
        if _psycopg3 is None and _psycopg2 is None:
            raise CommandError(
                "Neither 'psycopg' nor 'psycopg2' is installed. Install one of them to use this command."
            )

        host = os.environ.get("POSTGRES_HOST", "db")
        port = int(os.environ.get("POSTGRES_PORT", "5432"))
        user = os.environ.get("POSTGRES_USER", "postgres")
        password = os.environ.get("POSTGRES_PASSWORD", "")
        target_db = os.environ.get("POSTGRES_DB") or os.environ.get("PGDATABASE") or "job_hunting"
        maintenance_db = options["maintenance_db"]

        self.stdout.write(
            f"Checking database existence on {host}:{port} as user '{user}' "
            f"(maintenance DB: '{maintenance_db}', target DB: '{target_db}')"
        )

        # Connect to the maintenance DB
        try:
            if _psycopg3 is not None:
                conn = _psycopg3.connect(
                    dbname=maintenance_db,
                    host=host,
                    port=port,
                    user=user,
                    password=password,
                )
                conn.autocommit = True
                cur = conn.cursor()
            else:
                conn = _psycopg2.connect(
                    dbname=maintenance_db,
                    host=host,
                    port=port,
                    user=user,
                    password=password,
                )
                conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
                cur = conn.cursor()
        except Exception as e:
            raise CommandError(
                f"Unable to connect to maintenance database '{maintenance_db}' at {host}:{port} as '{user}': {e}"
            )

        try:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target_db,))
            exists = cur.fetchone() is not None
            if exists:
                self.stdout.write(self.style.SUCCESS(f"Database '{target_db}' already exists."))
                return

            # Quote identifier safely by doubling internal quotes
            safe_dbname = target_db.replace('"', '""')
            self.stdout.write(f"Creating database '{target_db}'...")
            cur.execute(f'CREATE DATABASE "{safe_dbname}"')
            self.stdout.write(self.style.SUCCESS(f"Database '{target_db}' created successfully."))
        except Exception as e:
            raise CommandError(f"Failed to ensure database '{target_db}': {e}")
        finally:
            try:
                cur.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
