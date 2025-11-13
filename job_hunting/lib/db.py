import os
import sys
import logging
import importlib
import importlib.util
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.exc import NoSuchModuleError
from job_hunting.lib.models.base import BaseModel

logger = logging.getLogger(__name__)

_engine = None
_session = None


def _build_db_url():
    """Build database URL from environment variables."""
    # Check for Django DATABASE_URL
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        database_url = database_url.strip()
        logger.info(f"Using DATABASE_URL environment variable (length: {len(database_url)})")
        return database_url

    # Default to localhost Postgres in development
    if os.environ.get("DEBUG", "False") == "True":
        logger.info("Using default development database URL (DEBUG=True)")
        return "postgresql://postgres:postgres@localhost:5432/job_hunting"
    
    # In production, DATABASE_URL must be set
    logger.error("DATABASE_URL environment variable not set in production")
    raise RuntimeError(
        "DATABASE_URL environment variable must be set in production. "
        "Example: postgresql://user:password@host:port/database"
    )


def init_sqlalchemy():
    global _engine, _session
    if _engine is not None and _session is not None:
        return

    try:
        db_url = _build_db_url()
        db_url = db_url.strip()
        
        # Auto-upgrade postgres:// to postgresql://
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)
            logger.info("Rewrote postgres:// to postgresql:// for compatibility")
        
        # Parse and validate the database URL
        from sqlalchemy.engine.url import make_url
        parsed_url = make_url(db_url)
        
        # Validate drivername
        if not parsed_url.drivername or parsed_url.drivername in (":", ""):
            logger.error(
                f"Invalid DATABASE_URL (no dialect found): '{parsed_url.drivername}'. "
                "Expected schemes like postgresql://, sqlite:///path.db, mysql+pymysql://..."
            )
            raise ValueError(f"Invalid database URL - no dialect found")
        
        # Log parsed URL components (sanitized)
        sanitized_url = str(parsed_url).replace(parsed_url.password or "", "***") if parsed_url.password else str(parsed_url)
        logger.info(f"Initializing SQLAlchemy with URL: {sanitized_url}")
        logger.info(f"Database dialect: {parsed_url.drivername}, host: {parsed_url.host or 'n/a'}, database: {parsed_url.database or 'n/a'}")
        
        # Check for missing DBAPI drivers
        _check_dbapi_driver(parsed_url.drivername)
        debug_mode = os.environ.get("DEBUG", "False") == "True"
        sql_echo_env = os.environ.get("SQLALCHEMY_ECHO")
        echo_flag = debug_mode if sql_echo_env is None else (sql_echo_env == "True")
        if echo_flag:
            logger.info("SQLAlchemy echo is enabled")

        # Configure engine with standard pool settings
        engine_kwargs = {
            "pool_pre_ping": True,
            "pool_size": 10,
            "max_overflow": 20,
            "pool_timeout": 10,
            "echo": echo_flag,
        }

        try:
            _engine = create_engine(db_url, **engine_kwargs)
        except (NoSuchModuleError, ModuleNotFoundError) as e:
            logger.error(
                f"Failed to create SQLAlchemy engine - missing driver for '{parsed_url.drivername}': {e}. "
                "If using PostgreSQL, ensure the URL starts with postgresql:// and install psycopg2-binary."
            )
            raise
        _session = scoped_session(
            sessionmaker(bind=_engine, autoflush=False, autocommit=False)
        )
        BaseModel.set_session(_session)
        
        logger.info(f"SQLAlchemy engine dialect: {_engine.dialect.name}")

        # Test a simple connection and log server info
        try:
            with _engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                logger.info("Database connection test succeeded")
                try:
                    server_version = getattr(conn.connection, "server_version", None)
                    if server_version:
                        logger.info(f"Database server version: {server_version}")
                except Exception:
                    pass
        except Exception as ce:
            logger.error(f"Database connection test failed: {ce}")
            if not (os.environ.get("DEBUG", "False") == "True"):
                raise

        # Log declared vs live tables and Alembic version (if present)
        try:
            declared = BaseModel.declared_tables()
            logger.info(f"Declared SQLAlchemy tables ({len(declared)}): {declared}")
        except Exception as de:
            logger.debug(f"Could not list declared tables: {de}")

        try:
            insp = inspect(_engine)
            live_tables = sorted(insp.get_table_names())
            logger.info(f"Live DB tables ({len(live_tables)}): {live_tables}")
            if "alembic_version" in live_tables:
                try:
                    with _engine.connect() as conn:
                        versions = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()
                        logger.info(f"Alembic versions: {[v[0] for v in versions]}")
                except Exception as ave:
                    logger.warning(f"Failed to read alembic_version: {ave}")
        except Exception as ie:
            logger.warning(f"Inspector failed: {ie}")

    except Exception as e:
        # Include drivername in error message if available
        drivername = "unknown"
        try:
            from sqlalchemy.engine.url import make_url
            parsed_url = make_url(db_url)
            drivername = parsed_url.drivername or "unknown"
        except:
            pass
        
        error_msg = f"Failed to initialize SQLAlchemy (dialect: {drivername}): {e}"
        # In production, fail fast on database connection issues
        if not os.environ.get("DEBUG", "False") == "True":
            raise RuntimeError(error_msg) from e
        else:
            # In development, log warning but allow startup
            logger.warning(error_msg)
            return



def _check_dbapi_driver(drivername):
    """Check if the required DBAPI driver is available and log guidance if missing."""
    if drivername.startswith("postgresql"):
        if "+psycopg2" in drivername or drivername == "postgresql":
            if not importlib.util.find_spec("psycopg2"):
                logger.error(
                    f"PostgreSQL driver 'psycopg2' not found for drivername '{drivername}'. "
                    "Install with: pip install psycopg2-binary"
                )
        elif "+psycopg" in drivername:
            if not importlib.util.find_spec("psycopg"):
                logger.error(
                    f"PostgreSQL driver 'psycopg' not found for drivername '{drivername}'. "
                    "Install with: pip install psycopg"
                )
    elif drivername.startswith("mysql+pymysql"):
        if not importlib.util.find_spec("pymysql"):
            logger.error(
                f"MySQL driver 'pymysql' not found for drivername '{drivername}'. "
                "Install with: pip install pymysql"
            )
    # sqlite requires no additional drivers


def ensure_sqlalchemy_schema(with_advisory_lock=True):
    """Create SQLAlchemy tables with optional advisory lock for PostgreSQL."""
    # Ensure engine and session are initialized
    init_sqlalchemy()

    if _engine is None:
        logger.error("SQLAlchemy engine not initialized")
        return

    # Import SQLAlchemy model modules individually to be resilient to failures
    model_modules = [
        "certification", "cover_letter", "description", "education", "experience",
        "experience_description", "profile", "project_description", "resume",
        "resume_certification", "resume_education", "resume_experience", 
        "resume_skill", "resume_summary", "scrape", "skill", "summary", "user",
        "company", "job_post", "application", "score", "project"
    ]
    
    successful_imports = []
    failed_imports = []
    
    for module_name in model_modules:
        try:
            importlib.import_module(f"job_hunting.lib.models.{module_name}")
            successful_imports.append(module_name)
        except Exception as e:
            failed_imports.append(f"{module_name}: {e}")
            logger.warning(f"Failed to import model module {module_name}: {e}")
    
    logger.info(f"Successfully imported {len(successful_imports)} model modules: {successful_imports}")
    if failed_imports:
        logger.warning(f"Failed to import {len(failed_imports)} model modules: {failed_imports}")

    tables_to_create = [
        t for t in BaseModel.metadata.sorted_tables if t.name != "auth_user"
    ]

    if not tables_to_create:
        logger.info("No SQLAlchemy tables to create")
        return

    logger.info(f"Tables to create: {[t.name for t in tables_to_create]}")
    # Snapshot live tables before create_all
    try:
        insp = inspect(_engine)
        live_before = sorted(insp.get_table_names())
        logger.info(f"Live DB tables before create_all ({len(live_before)}): {live_before}")
    except Exception as e:
        logger.warning(f"Could not inspect tables before create_all: {e}")

    # Use advisory lock for PostgreSQL to prevent concurrent schema creation
    if with_advisory_lock and _engine.dialect.name == "postgresql":
        # Use a constant key for the advisory lock
        SCHEMA_LOCK_KEY = 123456789

        with _engine.begin() as conn:
            try:
                # Acquire advisory lock
                result = conn.execute(
                    text("SELECT pg_advisory_lock(:k)"), {"k": SCHEMA_LOCK_KEY}
                )
                logger.info("Acquired PostgreSQL advisory lock for schema creation")

                # Create only intended tables with checkfirst=True
                BaseModel.metadata.create_all(
                    bind=conn, tables=tables_to_create, checkfirst=True
                )
                logger.info("SQLAlchemy schema creation completed")
                # Snapshot live tables after create_all
                try:
                    insp = inspect(conn)
                    live_after = sorted(insp.get_table_names())
                    logger.info(f"Live DB tables after create_all ({len(live_after)}): {live_after}")
                    if "alembic_version" in live_after:
                        try:
                            versions = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()
                            logger.info(f"Alembic versions: {[v[0] for v in versions]}")
                        except Exception as ave:
                            logger.warning(f"Failed to read alembic_version after create_all: {ave}")
                except Exception as e:
                    logger.warning(f"Could not inspect tables after create_all: {e}")

            finally:
                # Release advisory lock
                conn.execute(
                    text("SELECT pg_advisory_unlock(:k)"), {"k": SCHEMA_LOCK_KEY}
                )
                logger.info("Released PostgreSQL advisory lock")
    else:
        # For non-PostgreSQL or when lock is disabled
        with _engine.begin() as conn:
            BaseModel.metadata.create_all(bind=conn, tables=tables_to_create, checkfirst=True)
        logger.info("SQLAlchemy schema creation completed")
        # Snapshot live tables after create_all
        try:
            insp = inspect(_engine)
            live_after = sorted(insp.get_table_names())
            logger.info(f"Live DB tables after create_all ({len(live_after)}): {live_after}")
            if "alembic_version" in live_after:
                try:
                    with _engine.connect() as conn:
                        versions = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()
                        logger.info(f"Alembic versions: {[v[0] for v in versions]}")
                except Exception as ave:
                    logger.warning(f"Failed to read alembic_version after create_all: {ave}")
        except Exception as e:
            logger.warning(f"Could not inspect tables after create_all: {e}")
