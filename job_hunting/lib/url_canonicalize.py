"""Host-scoped URL rewriting for JobPost dedup.

Mirrors `agents/scrape_graph/url_canonicalize.py:apply_url_rewrites` so the
api side can canonicalize submitted links the same way the scrape pipeline
canonicalizes navigation targets — closing a dedup gap where the same
underlying job comes in via two URL forms (e.g. LinkedIn `/comm/jobs/view/`
from email vs. `/jobs/view/` from the browser extension) and produces two
JobPost rows.
"""
from __future__ import annotations

import re


def apply_url_rewrites(url: str, rules: list | None) -> str:
    """Apply a profile's `url_rewrites` regex list to `url`.

    `rules` items are `{"match": <regex>, "rewrite": <replacement>}`. First
    rule whose `match` regex actually changes the URL wins (re.sub semantics,
    so `\\1` backrefs work). Invalid regexes are skipped silently — a bad
    profile must not crash the dedup path.
    """
    if not url or not rules:
        return url
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        pattern = rule.get("match")
        replacement = rule.get("rewrite")
        if not pattern or replacement is None:
            continue
        try:
            new_url, n = re.subn(pattern, replacement, url)
        except re.error:
            continue
        if n > 0 and new_url != url:
            return new_url
    return url
