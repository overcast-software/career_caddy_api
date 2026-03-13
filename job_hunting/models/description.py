from django.db import models


class Description(models.Model):
    content = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "description"

    def __str__(self):
        return (self.content or "")[:50]
