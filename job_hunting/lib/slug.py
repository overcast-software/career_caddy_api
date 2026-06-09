"""String normalization helpers for Company-alias matching.

Phase A of the dedupe redesign (Plans/Dedupe Phase A — CompanyAlias).
The system was minting duplicate Company rows when an inbound capture
named the same entity with punctuation / case / dash drift —
"Allstate Corporation" vs "Allstate Insurance Company", or
"State Farm – Hartford" (U+2013 en-dash) vs "State Farm - Hartford"
(U+002D hyphen-minus).

``slug`` is the canonical normalization shared by Company.find_by_alias
and the migration backfill. It is deliberately aggressive:

- NFKC Unicode fold so visually-identical glyphs collapse
  (full-width letters → ASCII, ligatures → component letters).
- Unicode dashes / smart quotes → ASCII counterparts so the dash
  family (hyphen, en-dash, em-dash, minus sign, …) doesn't shard
  the slug space.
- Lowercase + collapse runs of whitespace.
- Strip every non-alphanumeric except the ASCII hyphen-minus.

``strip_corp_suffix`` peels common legal-entity suffixes
("Corporation", "Inc", "LLC", …) before slugging so
"Allstate Corporation" and "Allstate Insurance Company" both
collapse to the same head noun. Order of operations from the
caller side is always ``slug(strip_corp_suffix(raw))``.

Reference: parent plan ``go-over-this-plan-staged-sutherland.md``
Phase A § ``api submodule``; api notes.org ``Architecture/Dedupe
pipeline contract`` (this helper feeds the new
``Company.find_by_alias`` gate at ``job_post_extractor.py``).
"""

from __future__ import annotations

import re
import unicodedata


# Unicode dashes mapped to ASCII hyphen-minus. Covers the hyphen
# family (U+2010..U+2015) plus the math minus sign (U+2212), which
# LinkedIn historically renders into job titles.
_DASH_TRANSLATION = {
    0x2010: "-",  # ‐ hyphen
    0x2011: "-",  # ‑ non-breaking hyphen
    0x2012: "-",  # ‒ figure dash
    0x2013: "-",  # – en-dash
    0x2014: "-",  # — em-dash
    0x2015: "-",  # ― horizontal bar
    0x2212: "-",  # − minus sign
}

# Smart quotes mapped to their ASCII equivalents. Both single and
# double curly quotes appear in extracted job titles ("Driver's
# License", "Manager's Special").
_QUOTE_TRANSLATION = {
    0x2018: "'",  # ' left single quote
    0x2019: "'",  # ' right single quote
    0x201A: "'",  # ‚ single low-9 quote
    0x201B: "'",  # ‛ single high-reversed-9 quote
    0x201C: '"',  # " left double quote
    0x201D: '"',  # " right double quote
    0x201E: '"',  # „ double low-9 quote
    0x201F: '"',  # ‟ double high-reversed-9 quote
}

_TRANSLATION_TABLE = {**_DASH_TRANSLATION, **_QUOTE_TRANSLATION}

# Anything that survives the translation pass but isn't alphanumeric
# or a single ASCII hyphen-minus is stripped. We collapse runs of
# whitespace to a single ' ' first so multi-word names produce a
# single ' '-joined slug before final character filtering.
_NON_SLUG_CHARS_RE = re.compile(r"[^a-z0-9\- ]+")
_WHITESPACE_RUN_RE = re.compile(r"\s+")
_HYPHEN_OR_SPACE_RUN_RE = re.compile(r"[\s-]+")


# Legal-entity suffixes peeled by ``strip_corp_suffix``. Listed in
# longest-first order so multi-word suffixes ("insurance company",
# "holdings group") match before their single-word prefixes
# ("company", "holdings"). Match is case-insensitive and only fires
# when the suffix is a trailing token after the head noun — the
# regex anchors on \b at the start and \Z at the end.
_CORP_SUFFIXES = [
    "insurance company",
    "holdings group",
    "holding company",
    "corporation",
    "incorporated",
    "limited liability company",
    "limited partnership",
    "limited",
    "company",
    "holdings",
    "holding",
    "group",
    "corp",
    "co",
    "inc",
    "llc",
    "ltd",
    "lp",
    "llp",
    "plc",
    "ag",
    "gmbh",
    "sa",
    "nv",
    "bv",
]

# One regex that strips any number of trailing suffixes in a single
# pass. Wrap the suffix list in non-capturing alternation and allow
# trailing whitespace + punctuation between successive suffixes so
# "Acme Holdings, Inc." → "Acme" rather than "Acme Holdings,".
_SUFFIX_PATTERN = "|".join(re.escape(s) for s in _CORP_SUFFIXES)
_CORP_SUFFIX_RE = re.compile(
    rf"(?:[\s,.\-]+\b(?:{_SUFFIX_PATTERN})\b\.?)+\Z",
    flags=re.IGNORECASE,
)


def slug(s: str) -> str:
    """Return the normalized slug form of a company name.

    Idempotent: ``slug(slug(x)) == slug(x)``. Returns the empty
    string for empty / whitespace-only input.

    The result is suitable as a unique key — two inputs that should
    be treated as the same Company collapse to the same string. The
    inverse is NOT guaranteed (different real entities can share a
    slug); that's why ``CompanyAlias.name_slug`` is the dedup gate
    but the original ``name`` is preserved alongside.
    """
    if not s:
        return ""
    # NFKC fold first — collapses compatibility codepoints (full-width
    # forms, ligatures) before our translation table sees them.
    normalized = unicodedata.normalize("NFKC", s)
    # Map unicode dashes + smart quotes to ASCII.
    normalized = normalized.translate(_TRANSLATION_TABLE)
    # Lowercase + collapse whitespace runs.
    normalized = _WHITESPACE_RUN_RE.sub(" ", normalized.lower()).strip()
    # Strip everything that isn't [a-z0-9- ].
    normalized = _NON_SLUG_CHARS_RE.sub("", normalized)
    # Collapse runs of whitespace + hyphens into a single '-' so
    # "acme - corp" and "acme corp" and "acme--corp" all converge.
    normalized = _HYPHEN_OR_SPACE_RUN_RE.sub("-", normalized).strip("-")
    return normalized


def strip_corp_suffix(name: str) -> str:
    """Strip trailing legal-entity suffixes from a company name.

    Operates on the raw human-readable name (before ``slug``). Used by
    the caller as ``slug(strip_corp_suffix(name))`` so
    "Allstate Corporation" and "Allstate Insurance Company" both
    collapse to "allstate". Idempotent.

    Returns the input unchanged if no recognized suffix is present.
    """
    if not name:
        return ""
    stripped = name.strip()
    # Repeat until no more suffixes remain — "Acme Holdings Inc"
    # peels Inc → Holdings → Acme in a single regex call thanks to
    # the trailing ``+`` quantifier, but a defensive loop guards
    # against any future regex change.
    while True:
        new = _CORP_SUFFIX_RE.sub("", stripped).rstrip(" ,.–—-")
        if new == stripped or not new:
            return new or stripped
        stripped = new
