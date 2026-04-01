from django.db import models
from .base import GetMixin


class Experience(GetMixin, models.Model):
    title = models.CharField(max_length=255, null=True, blank=True)
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    content = models.TextField(null=True, blank=True)
    location = models.CharField(max_length=255, null=True, blank=True)
    summary = models.CharField(max_length=500, null=True, blank=True)
    company = models.ForeignKey(
        "Company",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="experiences",
    )

    class Meta:
        db_table = "experience"

    @property
    def date_range(self):
        start_date = self.start_date
        end_date = self.end_date
        if start_date and end_date:
            return f"{start_date} \u2013 {end_date}"
        elif start_date:
            return f"{start_date} \u2013 Present"
        return ""

    @property
    def descriptions(self):
        from job_hunting.models.experience_description import ExperienceDescription
        from job_hunting.models.description import Description

        desc_ids = list(
            ExperienceDescription.objects.filter(experience_id=self.id)
            .order_by("order")
            .values_list("description_id", flat=True)
        )
        desc_map = {d.id: d for d in Description.objects.filter(pk__in=desc_ids)}
        return [desc_map[did] for did in desc_ids if did in desc_map]

    def to_export_dict(self) -> dict:
        exp_dict = {}

        exp_dict["company"] = self.company.name if self.company_id else ""
        exp_dict["title"] = self.title or ""
        exp_dict["location"] = self.location or ""
        exp_dict["summary"] = self.summary

        start_date = self.start_date
        end_date = self.end_date
        exp_dict["start_date"] = str(start_date) if start_date else None
        exp_dict["end_date"] = str(end_date) if end_date else None

        if start_date and end_date:
            exp_dict["date_range"] = f"{start_date} \u2013 {end_date}"
        elif start_date:
            exp_dict["date_range"] = f"{start_date} \u2013 Present"
        else:
            exp_dict["date_range"] = ""

        descriptions = []
        try:
            from job_hunting.models.experience_description import ExperienceDescription
            from job_hunting.models.description import Description

            desc_ids = list(
                ExperienceDescription.objects.filter(experience_id=self.id)
                .order_by("order")
                .values_list("description_id", flat=True)
            )
            desc_map = {d.id: d for d in Description.objects.filter(pk__in=desc_ids)}
            for desc_id in desc_ids:
                desc = desc_map.get(desc_id)
                if desc and desc.content:
                    descriptions.append(str(desc.content).strip())
        except Exception:
            pass
        exp_dict["descriptions"] = descriptions

        return exp_dict
