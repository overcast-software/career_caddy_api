from django.apps import AppConfig

class JobHuntingConfig(AppConfig):
    name = "job_hunting"

    def ready(self):
        from .lib.db import init_sqlalchemy
        init_sqlalchemy()
