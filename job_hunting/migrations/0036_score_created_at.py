from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0035_ai_usage"),
    ]

    operations = [
        migrations.AddField(
            model_name="score",
            name="created_at",
            field=models.DateTimeField(auto_now_add=True, null=True),
        ),
    ]
