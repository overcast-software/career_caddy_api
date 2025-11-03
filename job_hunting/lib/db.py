import os
import sys
import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from job_hunting.lib.models.base import BaseModel

logger = logging.getLogger(__name__)

_engine = None
_session = None


def _is_management_command():
    cmds = {"migrate", "makemigrations", "collectstatic"}
    return any(arg in sys.argv for arg in cmds)


def _build_db_url():
    """Build database URL from environment variables or fallback to SQLite."""
    # Check for SQLAlchemy-specific URL first
    sqlalchemy_url = os.environ.get("SQLALCHEMY_DATABASE_URL")
    if sqlalchemy_url:
        return sqlalchemy_url

    # Check for Django DATABASE_URL
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return database_url

    # Fallback to SQLite for development/testing
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    db_path = os.path.join(project_root, "job_data.db")
    return f"sqlite:///{db_path}"


def init_sqlalchemy():
    global _engine, _session
    if _engine is not None and _session is not None:
        return

    try:
        db_url = _build_db_url()
        is_sqlite = db_url.startswith("sqlite:")

        # Configure connection arguments
        connect_args = {}
        engine_kwargs = {
            "pool_pre_ping": True,
        }

        if is_sqlite:
            connect_args = {"check_same_thread": False}
        else:
            # Only set pool settings for non-SQLite engines
            engine_kwargs.update(
                {
                    "pool_size": 10,
                    "max_overflow": 20,
                    "pool_timeout": 60,
                }
            )

        _engine = create_engine(db_url, connect_args=connect_args, **engine_kwargs)

        # Schema creation is now handled separately via ensure_sqlalchemy_schema()
        # to prevent race conditions during app startup

        _session = scoped_session(
            sessionmaker(bind=_engine, autoflush=False, autocommit=False)
        )
        BaseModel.set_session(_session)

    except Exception as e:
        error_msg = f"Failed to initialize SQLAlchemy: {e}"
        # In production, fail fast on database connection issues
        if not os.environ.get("DEBUG", "False") == "True":
            raise RuntimeError(error_msg) from e
        else:
            # In development, log warning but allow startup
            logger.warning(error_msg)
            return


def ensure_sqlalchemy_schema(with_advisory_lock=True):
    """Create SQLAlchemy tables with optional advisory lock for PostgreSQL."""
    # Ensure engine and session are initialized
    init_sqlalchemy()
    
    if _engine is None:
        logger.error("SQLAlchemy engine not initialized")
        return
    
    tables_to_create = [
        t for t in BaseModel.metadata.sorted_tables if t.name != "auth_user"
    ]
    
    if not tables_to_create:
        logger.info("No SQLAlchemy tables to create")
        return
    
    # Use advisory lock for PostgreSQL to prevent concurrent schema creation
    if with_advisory_lock and _engine.dialect.name == 'postgresql':
        # Use a constant key for the advisory lock
        SCHEMA_LOCK_KEY = 123456789
        
        with _engine.connect() as conn:
            try:
                # Acquire advisory lock
                result = conn.execute(
                    "SELECT pg_advisory_lock(%s)", (SCHEMA_LOCK_KEY,)
                )
                logger.info("Acquired PostgreSQL advisory lock for schema creation")
                
                # Create tables with checkfirst=True
                BaseModel.metadata.create_all(
                    bind=conn, tables=tables_to_create, checkfirst=True
                )
                logger.info("SQLAlchemy schema creation completed")
                
            finally:
                # Release advisory lock
                conn.execute(
                    "SELECT pg_advisory_unlock(%s)", (SCHEMA_LOCK_KEY,)
                )
                logger.info("Released PostgreSQL advisory lock")
    else:
        # For non-PostgreSQL or when lock is disabled
        BaseModel.metadata.create_all(
            bind=_engine, tables=tables_to_create, checkfirst=True
        )
        logger.info("SQLAlchemy schema creation completed (no lock)")
