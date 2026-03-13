from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('job_hunting', '0007_remove_summary_job_post_fk'),
    ]

    operations = [
        migrations.RunSQL(
            sql="ALTER TABLE summary DROP CONSTRAINT IF EXISTS summary_user_id_fkey;",
            reverse_sql="",
        ),
    ]
