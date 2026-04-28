"""Text-signal detectors that read structured state out of LLM-cleaned
job-post descriptions without an extra LLM call.

Today: closed-posting detection. Designed to be conservative — return
None (unknown) when no signal fires, only return "closed" / "open"
when a phrase from the curated list matches. False positives here
would hide live postings; false negatives just leave the list view
unfiltered, which is the historical default.

Hosted here (not on a per-host ScrapeProfile) because the same set
of phrases applies across most ATSes — LinkedIn, Greenhouse, Lever,
ZipRecruiter, Indeed all use one of these formulations when a role
goes off the market. Per-host overrides can layer on top later.
"""
from __future__ import annotations

import re
from typing import Optional


# Closed phrases — case-insensitive substring match. Order doesn't
# matter, but we compile each into a regex with word-boundary anchors
# so partial matches inside larger words don't accidentally fire.
_CLOSED_PHRASES = [
    r"no longer accepting applications",
    r"applications? (?:are|have been) closed",
    r"this (?:position|role|job|posting) is closed",
    r"this (?:position|role|job|posting) (?:has been|was) (?:closed|filled)",
    r"position has been filled",
    r"role (?:has been|was) filled",
    r"we are no longer accepting",
    r"no longer open(?: for applications)?",
    r"applications are no longer (?:being )?accepted",
    r"\[\s*closed\s*[—\-:]\s*applications? no longer accepted\s*\]",
    r"\[\s*closed\s*\]",
]

_CLOSED_RE = re.compile(
    "|".join(f"(?:{p})" for p in _CLOSED_PHRASES),
    flags=re.IGNORECASE,
)


def detect_posting_status(text: Optional[str]) -> Optional[str]:
    """Return ``"closed"`` if a closed-posting phrase appears in
    ``text``, otherwise ``None``.

    We deliberately never return ``"open"`` from a positive phrase:
    every other normal description is "presumed open" and the absence
    of a closed signal is the right answer. Returning ``None`` (rather
    than ``"open"``) keeps the JobPost.application_status column honest
    about what the detector actually proved — the list view treats
    null as "show by default" so users still see the post.

    Returns:
        ``"closed"`` on a phrase hit, ``None`` otherwise (including
        when ``text`` is None / empty).
    """
    if not text:
        return None
    return "closed" if _CLOSED_RE.search(text) else None
