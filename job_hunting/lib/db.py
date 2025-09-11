import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from job_hunting.lib.models.base import BaseModel

_engine = None
_session = None

def _build_db_url():
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    db_path = os.path.join(project_root, "job_data.db")
    return f"sqlite:///{db_path}"

def init_sqlalchemy():
    global _engine, _session
    if _engine is not None and _session is not None:
        return
    db_url = _build_db_url()
    connect_args = {"check_same_thread": False} if db_url.startswith("sqlite:") else {}
    _engine = create_engine(db_url, connect_args=connect_args)
    # Ensure tables exist in the SQLite file
    BaseModel.metadata.create_all(bind=_engine)
    _session = scoped_session(sessionmaker(bind=_engine, autoflush=False, autocommit=False))
    BaseModel.set_session(_session)
