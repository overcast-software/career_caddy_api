from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0020_wave3_join_table_django_models"),
    ]

    operations = [
        migrations.RenameModel(
            old_name="Application",
            new_name="JobApplication",
        ),
    ]
