import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from job_hunting.lib.models.base import BaseModel

_engine = None
_session = None


def _build_db_url():
    """Build database URL from environment variables or fallback to SQLite."""
    # Check for SQLAlchemy-specific URL first
    sqlalchemy_url = os.environ.get('SQLALCHEMY_DATABASE_URL')
    if sqlalchemy_url:
        return sqlalchemy_url
    
    # Check for Django DATABASE_URL
    database_url = os.environ.get('DATABASE_URL')
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
            'pool_pre_ping': True,
        }
        
        if is_sqlite:
            connect_args = {"check_same_thread": False}
        else:
            # Only set pool settings for non-SQLite engines
            engine_kwargs.update({
                'pool_size': 10,
                'max_overflow': 20,
                'pool_timeout': 60,
            })
        
        _engine = create_engine(
            db_url,
            connect_args=connect_args,
            **engine_kwargs
        )
        
        # Create tables if they don't exist (non-destructive only)
        # Note: This is not a migrations strategy - use Django migrations for schema changes
        BaseModel.metadata.create_all(bind=_engine)
        
        _session = scoped_session(
            sessionmaker(bind=_engine, autoflush=False, autocommit=False)
        )
        BaseModel.set_session(_session)
        
    except Exception as e:
        error_msg = f"Failed to initialize SQLAlchemy: {e}"
        # In production, fail fast on database connection issues
        if not os.environ.get('DEBUG', 'False') == 'True':
            raise RuntimeError(error_msg) from e
        else:
            # In development, log warning but allow startup
            import logging
            logging.getLogger(__name__).warning(error_msg)
            return
