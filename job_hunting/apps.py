import warnings
from django.apps import AppConfig

# Suppress deprecation warnings emitted when docxcompose imports pkg_resources
warnings.filterwarnings(
    "ignore",
    message="pkg_resources is deprecated as an API.*",
    category=UserWarning,
    module=r"docxcompose\.properties",
)


class JobHuntingConfig(AppConfig):
    name = "job_hunting"
    default_auto_field = "django.db.models.BigAutoField"
    verbose_name = "Job Hunting"
