from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0001_initial"),
    ]

    # No-op migration: Profile moved to SQLAlchemy; Django no longer manages this table.
    operations = []
