from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("job_hunting", "0016_job_post_django_model"),
    ]

    operations = [
        # Drop SA-created FKs from job_post table (now Django-managed)
        migrations.RunSQL(
            sql="ALTER TABLE IF EXISTS job_post DROP CONSTRAINT IF EXISTS job_post_created_by_fkey;",
            reverse_sql="",
        ),
        migrations.RunSQL(
            sql="ALTER TABLE IF EXISTS job_post DROP CONSTRAINT IF EXISTS job_post_company_id_fkey;",
            reverse_sql="",
        ),
        # Drop SA-created outgoing FKs from score/scrape/cover_letter/application to job_post
        migrations.RunSQL(
            sql="ALTER TABLE IF EXISTS score DROP CONSTRAINT IF EXISTS score_job_post_id_fkey;",
            reverse_sql="",
        ),
        migrations.RunSQL(
            sql="ALTER TABLE IF EXISTS scrape DROP CONSTRAINT IF EXISTS scrape_job_post_id_fkey;",
            reverse_sql="",
        ),
        migrations.RunSQL(
            sql="ALTER TABLE IF EXISTS cover_letter DROP CONSTRAINT IF EXISTS cover_letter_job_post_id_fkey;",
            reverse_sql="",
        ),
        migrations.RunSQL(
            sql="ALTER TABLE IF EXISTS application DROP CONSTRAINT IF EXISTS application_job_post_id_fkey;",
            reverse_sql="",
        ),
    ]
