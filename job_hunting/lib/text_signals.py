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


def jaccard_5gram(a: str, b: str) -> float:
    """Return Jaccard similarity of the 5-gram token sets of two strings.

    Tokens are lowercased words. Returns 0.0 when either input is empty.
    Used as a cheapness gate in DescriptionArbiter before calling the LLM.
    """
    def _ngrams(text: str, n: int = 5):
        tokens = re.sub(r"\s+", " ", text.lower()).split()
        return set(
            tuple(tokens[i : i + n]) for i in range(max(0, len(tokens) - n + 1))
        )

    a_set = _ngrams(a)
    b_set = _ngrams(b)
    if not a_set or not b_set:
        return 0.0
    return len(a_set & b_set) / len(a_set | b_set)


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
