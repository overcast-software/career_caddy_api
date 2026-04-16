from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('job_hunting', '0042_company_unique_name'),
    ]

    operations = [
        migrations.AlterField(
            model_name='company',
            name='name',
            field=models.CharField(max_length=255, unique=True),
        ),
    ]
