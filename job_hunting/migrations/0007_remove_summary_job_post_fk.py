from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('job_hunting', '0006_summary'),
    ]

    operations = [
        migrations.RunSQL(
            sql="ALTER TABLE summary DROP CONSTRAINT IF EXISTS summary_job_post_id_fkey;",
            reverse_sql="",
        ),
    ]
