import os
import logging
import warnings
from django.apps import AppConfig

# Suppress deprecation warnings emitted when docxcompose imports pkg_resources
warnings.filterwarnings(
    "ignore",
    message="pkg_resources is deprecated as an API.*",
    category=UserWarning,
    module=r"docxcompose\.properties",
)

logger = logging.getLogger(__name__)

class JobHuntingConfig(AppConfig):
    name = "job_hunting"
    default_auto_field = "django.db.models.BigAutoField"
    verbose_name = "Job Hunting"

    def ready(self):
        from .lib.db import init_sqlalchemy
        
        # Environment flag for strict initialization
        strict_init = os.environ.get('SQLALCHEMY_INIT_STRICT', 'True') == 'True'
        
        try:
            init_sqlalchemy()
            
            # Wrap Base.metadata methods to clear session before schema changes
            from .lib.models.base import BaseModel, Base
            
            orig_drop = Base.metadata.drop_all
            orig_create = Base.metadata.create_all
            
            def drop_all_reset(*a, **k):
                try:
                    BaseModel.clear_session()
                except Exception:
                    pass
                return orig_drop(*a, **k)
            
            def create_all_reset(*a, **k):
                try:
                    BaseModel.clear_session()
                except Exception:
                    pass
                return orig_create(*a, **k)
            
            Base.metadata.drop_all = drop_all_reset
            Base.metadata.create_all = create_all_reset
            
        except Exception as e:
            if strict_init:
                # Fail fast in production
                logger.error(f"SQLAlchemy initialization failed: {e}")
                raise
            else:
                # Log warning but allow startup in development/testing
                logger.warning(f"SQLAlchemy initialization failed, continuing: {e}")
