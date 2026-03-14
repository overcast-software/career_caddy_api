from django.db import migrations


class Migration(migrations.Migration):
    """
    Drop FK constraints from SA-managed tables that pointed to auth_user.
    auth_user is Django-owned; SA models should not maintain FK constraints to it.
    Uses ALTER TABLE IF EXISTS so this is safe on fresh test databases.
    """

    dependencies = [
        ("job_hunting", "0017_remove_job_post_sa_fkeys"),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            ALTER TABLE IF EXISTS resume DROP CONSTRAINT IF EXISTS resume_user_id_fkey;
            ALTER TABLE IF EXISTS application DROP CONSTRAINT IF EXISTS application_user_id_fkey;
            ALTER TABLE IF EXISTS cover_letter DROP CONSTRAINT IF EXISTS cover_letter_user_id_fkey;
            ALTER TABLE IF EXISTS score DROP CONSTRAINT IF EXISTS score_user_id_fkey;
            ALTER TABLE IF EXISTS project DROP CONSTRAINT IF EXISTS project_user_id_fkey;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
