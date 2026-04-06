from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('job_hunting', '0030_add_status_to_score'),
    ]

    operations = [
        migrations.AddField(
            model_name='question',
            name='job_post',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='direct_questions',
                to='job_hunting.jobpost',
            ),
        ),
    ]
