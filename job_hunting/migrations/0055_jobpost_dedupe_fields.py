import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from django.db import migrations, models


_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gh_src", "gh_jid",
    "lever-source", "lever-origin",
    "trk", "refid", "trackingid",
    "source", "src",
}

_WS = re.compile(r"\s+")


def _canonicalize(url):
    if not url:
        return None
    try:
        u = urlparse(url)
    except ValueError:
        return url
    kept = [
        (k, v)
        for k, v in parse_qsl(u.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    return urlunparse(u._replace(query=urlencode(kept), fragment=""))


def _fingerprint(company_id, title, location):
    if not (company_id and title):
        return None
    parts = [
        str(company_id),
        _WS.sub(" ", title.strip().lower()),
        _WS.sub(" ", (location or "").strip().lower()),
    ]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()


def backfill(apps, schema_editor):
    JobPost = apps.get_model("job_hunting", "JobPost")
    batch = []
    for jp in JobPost.objects.all().iterator(chunk_size=500):
        jp.canonical_link = _canonicalize(jp.link)
        jp.content_fingerprint = _fingerprint(jp.company_id, jp.title, jp.location)
        batch.append(jp)
        if len(batch) >= 500:
            JobPost.objects.bulk_update(batch, ["canonical_link", "content_fingerprint"])
            batch.clear()
    if batch:
        JobPost.objects.bulk_update(batch, ["canonical_link", "content_fingerprint"])


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0054_scrapeprofile_url_rewrites"),
    ]

    operations = [
        migrations.AddField(
            model_name="jobpost",
            name="canonical_link",
            field=models.CharField(blank=True, db_index=True, max_length=1000, null=True),
        ),
        migrations.AddField(
            model_name="jobpost",
            name="content_fingerprint",
            field=models.CharField(blank=True, max_length=40, null=True),
        ),
        migrations.AddField(
            model_name="jobpost",
            name="duplicate_of",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.SET_NULL,
                related_name="duplicates",
                to="job_hunting.jobpost",
            ),
        ),
        migrations.AddIndex(
            model_name="jobpost",
            index=models.Index(
                fields=["content_fingerprint", "-created_at"],
                name="jobpost_fp_recent_idx",
            ),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
