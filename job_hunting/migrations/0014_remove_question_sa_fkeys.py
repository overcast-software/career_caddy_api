from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("job_hunting", "0013_question_django_model"),
    ]

    operations = [
        migrations.RunSQL(
            sql="ALTER TABLE question DROP CONSTRAINT IF EXISTS question_application_id_fkey;",
            reverse_sql="",
        ),
        migrations.RunSQL(
            sql="ALTER TABLE question DROP CONSTRAINT IF EXISTS question_company_id_fkey;",
            reverse_sql="",
        ),
        migrations.RunSQL(
            sql="ALTER TABLE question DROP CONSTRAINT IF EXISTS question_created_by_id_fkey;",
            reverse_sql="",
        ),
    ]
