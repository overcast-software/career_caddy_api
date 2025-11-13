import os
import sys
import logging
import importlib
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, scoped_session
from job_hunting.lib.models.base import BaseModel

logger = logging.getLogger(__name__)

_engine = None
_session = None


def _build_db_url():
    """Build database URL from environment variables."""
    # Check for Django DATABASE_URL
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return database_url

    # Default to localhost Postgres in development
    if os.environ.get("DEBUG", "False") == "True":
        return "postgresql://postgres:postgres@localhost:5432/job_hunting"
    
    # In production, DATABASE_URL must be set
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
        
        # Log the resolved database URL (sanitized)
        from sqlalchemy.engine.url import make_url
        parsed_url = make_url(db_url)
        sanitized_url = str(parsed_url).replace(parsed_url.password or "", "***") if parsed_url.password else str(parsed_url)
        logger.info(f"Initializing SQLAlchemy with URL: {sanitized_url}")

        # Configure engine with standard pool settings
        engine_kwargs = {
            "pool_pre_ping": True,
            "pool_size": 10,
            "max_overflow": 20,
            "pool_timeout": 10,
        }

        _engine = create_engine(db_url, **engine_kwargs)
        _session = scoped_session(
            sessionmaker(bind=_engine, autoflush=False, autocommit=False)
        )
        BaseModel.set_session(_session)
        
        logger.info(f"SQLAlchemy engine dialect: {_engine.dialect.name}")

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

    # Use advisory lock for PostgreSQL to prevent concurrent schema creation
    if with_advisory_lock and _engine.dialect.name == "postgresql":
        # Use a constant key for the advisory lock
        SCHEMA_LOCK_KEY = 123456789

        with _engine.connect() as conn:
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

            finally:
                # Release advisory lock
                conn.execute(
                    text("SELECT pg_advisory_unlock(:k)"), {"k": SCHEMA_LOCK_KEY}
                )
                logger.info("Released PostgreSQL advisory lock")
    else:
        # For non-PostgreSQL or when lock is disabled
        BaseModel.metadata.create_all(bind=_engine, tables=tables_to_create, checkfirst=True)
        logger.info("SQLAlchemy schema creation completed")
