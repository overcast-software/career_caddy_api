from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('job_hunting', '0053_scrapestatus_graph_node_scrapestatus_graph_payload'),
    ]

    operations = [
        migrations.AddField(
            model_name='scrapeprofile',
            name='url_rewrites',
            field=models.JSONField(blank=True, null=True),
        ),
    ]
