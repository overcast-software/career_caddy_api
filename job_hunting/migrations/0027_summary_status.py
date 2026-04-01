from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0026_cover_letter_state_answer_state"),
    ]

    operations = [
        migrations.AddField(
            model_name="summary",
            name="status",
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
    ]
