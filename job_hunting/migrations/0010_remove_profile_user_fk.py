from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('job_hunting', '0009_profile'),
    ]

    operations = [
        migrations.RunSQL(
            sql="ALTER TABLE profile DROP CONSTRAINT IF EXISTS profile_user_id_fkey;",
            reverse_sql="",
        ),
    ]
