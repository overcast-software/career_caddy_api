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
            from job_hunting.lib.models.base import BaseModel
            
            # Show current engine info
            try:
                session = BaseModel.get_session()
                if session and session.bind:
                    engine_url = str(session.bind.url)
                    # Sanitize password if present
                    if session.bind.url.password:
                        engine_url = engine_url.replace(session.bind.url.password, "***")
                    self.stdout.write(f"Target database: {engine_url}")
                    self.stdout.write(f"Current metadata tables: {list(BaseModel.metadata.tables.keys())}")
            except Exception as e:
                self.stdout.write(f"Could not retrieve engine info: {e}")
            
            ensure_sqlalchemy_schema(with_advisory_lock=use_lock)
            
            # Show final metadata tables
            try:
                self.stdout.write(f"Final metadata tables: {list(BaseModel.metadata.tables.keys())}")
            except Exception as e:
                self.stdout.write(f"Could not retrieve final metadata: {e}")
                
            self.stdout.write(
                self.style.SUCCESS("SQLAlchemy schema initialization completed successfully")
            )
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f"Failed to initialize SQLAlchemy schema: {e}")
            )
            raise
