from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("job_hunting", "0014_remove_question_sa_fkeys"),
    ]

    operations = [
        migrations.RunSQL(
            sql="ALTER TABLE IF EXISTS answer DROP CONSTRAINT IF EXISTS answer_question_id_fkey;",
            reverse_sql="",
        ),
    ]
