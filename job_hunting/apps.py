import os
import logging
from django.apps import AppConfig

logger = logging.getLogger(__name__)

class JobHuntingConfig(AppConfig):
    name = "job_hunting"

    def ready(self):
        from .lib.db import init_sqlalchemy
        
        # Environment flag for strict initialization
        strict_init = os.environ.get('SQLALCHEMY_INIT_STRICT', 'True') == 'True'
        
        try:
            init_sqlalchemy()
        except Exception as e:
            if strict_init:
                # Fail fast in production
                logger.error(f"SQLAlchemy initialization failed: {e}")
                raise
            else:
                # Log warning but allow startup in development/testing
                logger.warning(f"SQLAlchemy initialization failed, continuing: {e}")
