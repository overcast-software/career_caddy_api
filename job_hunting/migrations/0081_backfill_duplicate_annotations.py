"""Backfill DuplicateAnnotation rows for every existing JobPost with a
duplicate_of pointer set before Phase 3 audit shipped.

Each historical row gets action='historical' so the dedupe-feedback
report can distinguish them from genuine human decisions captured by
the new verb endpoints. set_by is best-effort (the JobPost's
created_by, since we have no record of who first linked the rows);
set_at falls back to JobPost.created_at when present, else None.

Idempotent: skips JobPosts that already have a historical annotation.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    JobPost = apps.get_model("job_hunting", "JobPost")
    DuplicateAnnotation = apps.get_model("job_hunting", "DuplicateAnnotation")

    already = set(
        DuplicateAnnotation.objects.filter(action="historical")
        .values_list("from_jp_id", flat=True)
    )

    rows = []
    qs = JobPost.objects.filter(duplicate_of_id__isnull=False).only(
        "id", "duplicate_of_id", "created_by_id", "created_at"
    )
    for jp in qs.iterator():
        if jp.id in already:
            continue
        rows.append(
            DuplicateAnnotation(
                from_jp_id=jp.id,
                to_jp_id=jp.duplicate_of_id,
                previous_to_id=None,
                action="historical",
                set_by_id=jp.created_by_id,
                signal_state={},
            )
        )
    if rows:
        DuplicateAnnotation.objects.bulk_create(rows, batch_size=1000)


def reverse(apps, schema_editor):
    DuplicateAnnotation = apps.get_model("job_hunting", "DuplicateAnnotation")
    DuplicateAnnotation.objects.filter(action="historical").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0080_duplicateannotation"),
    ]

    operations = [
        migrations.RunPython(backfill, reverse),
    ]
