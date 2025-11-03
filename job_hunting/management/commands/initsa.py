from django.core.management.base import BaseCommand
from job_hunting.lib.db import ensure_sqlalchemy_schema


class Command(BaseCommand):
    help = "Initialize SQLAlchemy schema (create tables)"

    def add_arguments(self, parser):
        parser.add_argument(
            '--no-lock',
            action='store_true',
            help='Skip PostgreSQL advisory lock during schema creation',
        )

    def handle(self, *args, **options):
        use_lock = not options['no_lock']
        
        self.stdout.write("Initializing SQLAlchemy schema...")
        
        try:
            ensure_sqlalchemy_schema(with_advisory_lock=use_lock)
            self.stdout.write(
                self.style.SUCCESS("SQLAlchemy schema initialization completed successfully")
            )
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f"Failed to initialize SQLAlchemy schema: {e}")
            )
            raise
