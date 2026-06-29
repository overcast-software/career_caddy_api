"""Index JobPost.apply_url for the popup-open exact-link lookup (BACK #87).

Background: the browser extension's "is this URL already tracked?"
check hits ``GET /api/v1/job-posts/?filter[link]=<url>``. That branch in
``JobPostViewSet.list`` ORs four equality legs:

    Q(link=x) | Q(apply_url=x) | Q(canonical_link=canon) | Q(apply_url=canon)

``link`` (UNIQUE) and ``canonical_link`` (db_index) are btree-indexed,
but ``apply_url`` had no index at all. An OR predicate can only become a
BitmapOr when *every* leg is index-satisfiable; one unindexed leg forces
the planner to evaluate the whole predicate with a single sequential
scan over the entire job_post table. On the platform-wide prod table
that full scan was the multi-second latency the extension popup saw.

EXPLAIN proof (dev, 1989 rows) — even with ``enable_seqscan = off`` the
planner still chose ``Seq Scan ... Rows Removed by Filter: 1989`` because
no index could satisfy the ``apply_url`` legs.

Fix: add an index so the planner can BitmapOr all four legs.

Why HashIndex, not a plain btree/db_index:
  - The lookup only ever does equality (``apply_url = <value>``); hash
    indexes support exactly ``=`` and nothing else, which is all we need.
  - ``apply_url`` is ``CharField(max_length=2000)``. A btree index entry
    must fit btree's ~2704-byte per-tuple ceiling; a multibyte URL near
    the 2000-char limit can exceed that and ERROR on INSERT. A hash index
    stores a 32-bit hash of the value, so it has no such length ceiling —
    bulletproof against pathologically long apply destinations.

Hash indexes are WAL-logged and crash-safe since PostgreSQL 10.

Reversibility: AddIndex reverses cleanly via RemoveIndex (Django handles
the reverse automatically). Pure schema; no data step.

This migration deliberately carries ONLY the AddIndex operation. Running
``makemigrations`` also surfaced unrelated index-rename / BigAutoField
state drift from the Django 5.2 upgrade; that is a separate concern and
is intentionally NOT bundled here.
"""

import django.contrib.postgres.indexes
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("job_hunting", "0129_userjobpost"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="jobpost",
            index=django.contrib.postgres.indexes.HashIndex(
                fields=["apply_url"], name="jobpost_apply_url_hash_idx"
            ),
        ),
    ]
