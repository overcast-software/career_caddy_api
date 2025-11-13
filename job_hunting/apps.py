import os
import logging
import warnings
from django.apps import AppConfig
from django.db.models.signals import post_migrate

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

    def import_models(self):
        # Import default models and also our extra profile_models module
        super().import_models()

    def ready(self):
        import sys
        from .lib.db import init_sqlalchemy, ensure_sqlalchemy_schema

        # Environment flag for strict initialization
        strict_init = os.environ.get("SQLALCHEMY_INIT_STRICT", "True") == "True"

        try:
            init_sqlalchemy()

            # Wrap Base.metadata methods to clear session before schema changes
            from .lib.models.base import BaseModel

            orig_drop = BaseModel.metadata.drop_all
            orig_create = BaseModel.metadata.create_all

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

            # Base.metadata.drop_all = drop_all_reset
            # BaseModel.metadata.create_all = create_all_reset
            BaseModel.metadata.create_all
            
            # Post-migrate hook for SQLAlchemy schema creation during tests
            if os.environ.get("SA_SCHEMA_ON_POST_MIGRATE", "False") == "True":
                def create_sa_schema_handler(sender, **kwargs):
                    try:
                        logger.info("Creating SQLAlchemy schema via post_migrate hook")
                        ensure_sqlalchemy_schema(with_advisory_lock=True)
                    except Exception as e:
                        logger.warning(f"SQLAlchemy schema creation in post_migrate failed: {e}")
                
                post_migrate.connect(create_sa_schema_handler, dispatch_uid="sa_schema_on_post_migrate")
            
            # Optional auto-init schema (disabled by default)
            schema_commands = {"migrate", "makemigrations", "collectstatic", "initsa"}
            is_schema_command = any(arg in sys.argv for arg in schema_commands)
            auto_init_enabled = os.environ.get("AUTO_INIT_SQLALCHEMY", "False") == "True"
            
            if not is_schema_command and auto_init_enabled:
                try:
                    logger.info("Auto-initializing SQLAlchemy schema")
                    ensure_sqlalchemy_schema(with_advisory_lock=True)
                except Exception as e:
                    logger.warning(f"Auto-init of SQLAlchemy schema failed: {e}")
                    
        except Exception as e:
            if strict_init:
                # Fail fast in production
                logger.error(f"SQLAlchemy initialization failed: {e}")
                raise
            else:
                # Log warning but allow startup in development/testing
                logger.warning(f"SQLAlchemy initialization failed, continuing: {e}")
