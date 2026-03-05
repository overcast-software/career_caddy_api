from django.core.management.base import BaseCommand
from job_hunting.lib.db import ensure_sqlalchemy_schema


class Command(BaseCommand):
    help = 'Create SQLAlchemy tables in the database'

    def handle(self, *args, **options):
        self.stdout.write('Creating SQLAlchemy tables...')
        try:
            ensure_sqlalchemy_schema()
            self.stdout.write(self.style.SUCCESS('Successfully created SQLAlchemy tables'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Failed to create tables: {e}'))
            raise
