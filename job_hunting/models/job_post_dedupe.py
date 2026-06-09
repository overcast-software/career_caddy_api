"""Duplicate detection for JobPost.

Separate from the apply-resolver (``apply_url`` / ``apply_url_status``).
Dedupe reads ``link`` only; the resolver reads/writes ``apply_url`` only.
The two code paths do not share helpers.
"""
from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from job_hunting.lib.slug import slug
from job_hunting.lib.url_canonicalize import apply_url_rewrites


# Source-of-truth ranking. Higher value beats lower when two writes for
# the same canonical_link / fingerprint collide. The extension is treated
# as the most-authoritative because the user verified the page in their
# own browser before pushing it; email-pipeline writes are LLM-extracted
# from third-party digests and routinely hallucinate (the jp 1724 SNBL
# incident). When a higher-trust source lands on top of a lower-trust
# existing post, we overwrite the post in place and write a
# JobPostOverwriteDecision audit row.
SOURCE_TRUST = {
    "extension": 100,
    "paste": 80,
    # User-forwarded mail (catchall ingest, Phase 2.5). Distinct from
    # `email` / `email_direct` (LLM-extracted from third-party digests,
    # trust 20): the user *themselves* chose to forward this listing,
    # so it carries human-attested intent. Slotted between `paste` (80)
    # and `scrape` (70) — higher trust than a fresh-page scrape because
    # the user vouched for the link, lower than a paste because the
    # body went through a mail-client's HTML mangler before us.
    "email-forward": 75,
    "scrape": 70,
    "redirect": 60,
    "manual": 50,
    "email": 20,
    "email_direct": 20,
}


def source_trust(source: str | None) -> int:
    """Return a trust score for a JobPost source. Unknown → manual (50)."""
    return SOURCE_TRUST.get(source or "manual", SOURCE_TRUST["manual"])


def prefer_extension_direct_link(
    existing_jp,
    incoming_scrape,
    incoming_link: str | None,
) -> str | None:
    """Return the link that should win on a canonical-collision merge.

    Phase A of the Extension direct-POST plan. Tie-breaker rule: the
    URL the user actually saw their browser render is more trustworthy
    than a server-side-only fetched URL — the extension's content-
    script can only fire on a tab the user navigated to. Background
    scrapes routinely land on tracker / SSO-wrapped variants that the
    user never sees rendered.

    Rule (in order):

    - If the *incoming* scrape is ``source_mode='extension-direct'``,
      its link wins.
    - Otherwise, if the *existing* JobPost's most-recent linked scrape
      was ``source_mode='extension-direct'``, the existing link wins
      (returned unchanged).
    - Otherwise, return ``incoming_link`` so the existing trust-rank
      logic in ``_trust_aware_overwrite`` keeps its current behavior
      (overwrite to the new URL on a higher-trust source flip).

    ``JobPost.source`` does NOT carry the extension-direct signal —
    that field records WHAT KIND of write created the JP (extension /
    paste / email / …) and stays ``extension`` for both browser-mode
    and extension-direct scrapes per the plan's orthogonal-axes note.
    HOW the row was captured lives on ``Scrape.source_mode``, which is
    why this helper joins through the scrape rather than reading off
    the JP.

    Returns the link string to keep, or ``None`` when ``incoming_link``
    is None (caller suppresses the link-overwrite path entirely in
    that case — same shape ``_trust_aware_overwrite`` already uses).
    """
    if not incoming_link:
        return None

    incoming_mode = getattr(incoming_scrape, "source_mode", None) if incoming_scrape else None
    if incoming_mode == "extension-direct":
        return incoming_link

    # Look up the existing JP's most-recent extension-direct scrape.
    # Walking the reverse relation is cheap — JobPost.scrapes is the
    # related_name on Scrape.job_post, indexed implicitly via the FK.
    existing_scrape_qs = getattr(existing_jp, "scrapes", None)
    if existing_scrape_qs is not None:
        try:
            existing_has_extension_direct = existing_scrape_qs.filter(
                source_mode="extension-direct"
            ).exists()
        except Exception:
            existing_has_extension_direct = False
        if existing_has_extension_direct:
            # Existing JP carries an extension-direct provenance —
            # preserve its link rather than overwriting with the
            # browser-mode incoming URL. Callers compare the return
            # value against ``existing_jp.link`` and skip the
            # overwrite when they're equal, so this naturally no-ops.
            return existing_jp.link

    return incoming_link


_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gh_src", "gh_jid",
    "lever-source", "lever-origin",
    "trk", "refid", "trackingid",
    "lk", "lvk", "tsid",
}
# Note: bare =source= and =src= were here previously but stripping them
# overreaches — many job boards use =?source=...= as a *functional*
# query param (worksourcewa.com encodes part of the job identifier
# there). Without them in the strip list, /jobview/?source=A and
# /jobview/?source=B canonicalize to distinct URLs as intended.

_WS = re.compile(r"\s+")

# Closing delimiters that LLM URL extractors (and naive regex captures)
# routinely include when they pull a URL out of HTML or markdown — they
# eat the matching close quote / paren. None of these characters belong
# at the END of a real URL, so a trailing run of any of them is parser
# slop. Strip defensively on every save. Whitespace is folded in for the
# same reason. Forward slashes and ? # & = are conspicuously absent
# because they're legitimate URL terminators on some sites.
_URL_TRAILING_JUNK_CHARS = "\"'<>()[]{}`, \t\n\r"


def strip_url_trailing_junk(url: str | None) -> str | None:
    """Return ``url`` with trailing HTML/markdown delimiter junk removed.

    The 2026-05-27 hiring.cafe JP 2981 incident — link stored as
    ``https://hiring.cafe/job/5fsbbgitg82ev1ar"`` because the LLM
    extractor in cc_auto captured the closing ``"`` from the HTML
    attribute — is the canonical case. None of the parsing layers
    downstream (urlparse, canonicalize_link, JobPost.save) noticed; the
    frontend then URL-encoded the literal ``"`` to ``%22`` and the
    destination 404'd.

    Idempotent; returns the input unchanged when nothing to strip; falls
    back to the original input when stripping would empty the string.
    """
    if not url:
        return url
    stripped = url.rstrip(_URL_TRAILING_JUNK_CHARS)
    return stripped if stripped else url


@lru_cache(maxsize=256)
def _profile_url_rewrites_for_host(host: str) -> tuple:
    """Return the host's `url_rewrites` rules as a hashable tuple.

    Cached per-process. Profile edits won't be picked up until restart —
    acceptable today (rewrites change rarely, and prod restarts on every
    deploy). If that becomes a problem, swap to a TTL cache or wire a
    post_save signal on ScrapeProfile to call `cache_clear()`.

    Reads both top-level `url_rewrites` and `css_selectors.url_rewrites`
    because some legacy profiles authored the rules inside `css_selectors`
    (the agents-side LoadProfile flattens that blob); top-level wins when
    both exist and are non-empty.
    """
    if not host:
        return ()
    try:
        from job_hunting.models.scrape_profile import ScrapeProfile
        profile = ScrapeProfile.objects.filter(hostname=host).first()
    except Exception:
        return ()
    if not profile:
        return ()
    rules = profile.url_rewrites
    if not rules and isinstance(profile.css_selectors, dict):
        rules = profile.css_selectors.get("url_rewrites")
    if not isinstance(rules, list) or not rules:
        return ()
    return tuple(
        (r.get("match"), r.get("rewrite"))
        for r in rules
        if isinstance(r, dict) and r.get("match") and r.get("rewrite") is not None
    )


def _rewrite_via_profile(url: str) -> str:
    """Apply the host's ScrapeProfile.url_rewrites to `url`, if any."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return url
    if host.startswith("www."):
        host = host[4:]
    rules_tuple = _profile_url_rewrites_for_host(host)
    if not rules_tuple:
        return url
    rules = [{"match": m, "rewrite": r} for m, r in rules_tuple]
    return apply_url_rewrites(url, rules)


def canonicalize_link(url: str | None) -> str | None:
    """Apply host-specific path rewrites then strip tracking query params.

    Two-stage:
    1. Look up `ScrapeProfile.url_rewrites` for the URL's host and apply
       the first matching regex rule. This collapses host-specific URL
       variants (e.g. LinkedIn `/comm/jobs/view/` → `/jobs/view/`) onto a
       single canonical form so dedup recognises them as the same job.
    2. Strip known tracking query params (utm_*, gh_*, etc.) and the
       fragment. Opaque path tokens (e.g. ziprecruiter `/ekm/<token>`)
       are left alone — the token IS the identifier on those hosts.

    Returns None for falsy input.
    """
    if not url:
        return None
    url = strip_url_trailing_junk(url)
    url = _rewrite_via_profile(url)
    try:
        u = urlparse(url)
    except ValueError:
        return url
    kept = [
        (k, v)
        for k, v in parse_qsl(u.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
    ]
    # Normalize trailing slash on non-root paths so two URLs that differ
    # only by a final `/` collapse to one canonical form. The 2026-05-27
    # JP 715 vs JP 2963 LinkedIn pair is the regression case — both
    # carried the same job id but one canonical_link ended in `/` and
    # the other did not, so the stage-1 exact match in find_duplicate
    # missed. Preserve a bare `/` (root) since stripping it would lose
    # the path delimiter.
    path = u.path.rstrip("/") if u.path and u.path != "/" else u.path
    return urlunparse(u._replace(path=path, query=urlencode(kept), fragment=""))


def fingerprint(post) -> str | None:
    """sha1(company_id | normalized_title | normalized_location).

    Returns None when company_id or title is missing — null-fingerprint
    rows skip dedupe. Description is intentionally NOT hashed: recruiter
    tweaks break the match without changing the underlying role.
    """
    if not (getattr(post, "company_id", None) and post.title):
        return None
    parts = [
        str(post.company_id),
        _WS.sub(" ", post.title.strip().lower()),
        _WS.sub(" ", (post.location or "").strip().lower()),
    ]
    return hashlib.sha1(
        "|".join(parts).encode(), usedforsecurity=False
    ).hexdigest()


def normalized_fingerprint(post) -> str | None:
    """sha1(company_id | slug(title) | slug(location)) — Phase B sibling
    of ``fingerprint``.

    Same shape and null-skip semantics as ``fingerprint``, but with the
    title and location passed through ``job_hunting.lib.slug.slug``
    (NFKC fold, unicode-dash/quote fold, lowercase, strip punctuation
    except ASCII hyphen-minus, collapse whitespace + hyphen runs).

    Kills punctuation drift that the case+whitespace-only normalization
    in ``fingerprint`` misses. The canonical regression:

    - JP 1329 "Software Engineer - Product Security" (U+002D hyphen)
      vs JP 3323 "Software Engineer – Product Security" (U+2013 en-dash)
      — visually identical, same role, but ``fingerprint`` produced
      different hashes because the en-dash survives the lowercase pass.
      ``normalized_fingerprint`` folds the dash family to a single
      ASCII hyphen-minus so both rows collapse.

    Additive, not replacement — ``fingerprint`` stays the primary
    signal (rollback path + the indexed column today). Phase B widens
    the fingerprint stage in ``find_duplicate`` to OR both columns;
    once the new column is trusted in prod the old one can be retired
    as a separate future ticket.
    """
    if not (getattr(post, "company_id", None) and post.title):
        return None
    parts = [
        str(post.company_id),
        slug(post.title),
        slug(post.location or ""),
    ]
    return hashlib.sha1(
        "|".join(parts).encode(), usedforsecurity=False
    ).hexdigest()


def find_apply_url_matches(post, base_qs=None):
    """Return JobPosts duplicating `post` via apply_url reciprocity.

    Two reciprocal queries on the cross-platform "apply destination" link:
    - Forward: an existing post's ``apply_url`` matches the incoming
      post's ``link`` or ``canonical_link`` (jobboard posted earlier,
      direct ATS landing followed).
    - Reverse: an existing post's ``link`` or ``canonical_link`` matches
      the incoming post's ``apply_url`` (direct ATS posted earlier,
      jobboard captured later).

    Returns a distinct queryset. Callers handle pk-exclusion, ordering,
    the ``.canonical`` chain walk, and visibility — pass a pre-filtered
    ``base_qs`` for the last.

    Shared primitive: ``find_duplicate`` uses this as a decision signal;
    ``compute_duplicate_candidates`` uses it as the ``apply_hint`` panel
    signal. Keep behavior identical across both call sites.
    """
    from django.db.models import Q

    from .job_post import JobPost

    if base_qs is None:
        base_qs = JobPost.objects.all()

    link_targets = [v for v in {post.link, post.canonical_link} if v]
    q_parts = []
    if link_targets:
        q_parts.append(Q(apply_url__in=link_targets))
    if post.apply_url:
        q_parts.append(Q(link=post.apply_url) | Q(canonical_link=post.apply_url))

    if not q_parts:
        return base_qs.none()

    q = q_parts[0]
    for part in q_parts[1:]:
        q |= part
    return base_qs.filter(q).distinct()


def find_duplicate(post, window_days: int = 30):
    """Return an existing JobPost this one duplicates, or None.

    Called before insert from creation paths. Also safe to call after a
    scrape enriches a previously-null-fingerprint row — the hold-poller
    uses it for a post-scrape re-check.
    """
    from django.utils import timezone

    from .job_post import JobPost

    if post.canonical_link:
        hit = (
            JobPost.objects
            .filter(canonical_link=post.canonical_link)
            .exclude(pk=post.pk)
            .order_by("created_at")
            .first()
        )
        if hit:
            return hit.canonical

    hit = (
        find_apply_url_matches(post)
        .exclude(pk=post.pk)
        .order_by("created_at")
        .first()
    )
    if hit:
        return hit.canonical

    if post.content_fingerprint or post.normalized_fingerprint:
        from django.db.models import Q

        cutoff = timezone.now() - timedelta(days=window_days)
        # Rolling window on ``last_seen_at`` (NOT ``created_at``). The
        # column is bumped on every dedupe hit / scrape attach / merge
        # so a role that keeps being re-seen on different channels
        # stays in-window past the literal 30-day cutoff from its first
        # capture. The JP 1329 Allstate case (42 days old, rescraped
        # from a different host) regresses without this — fingerprint
        # match is the only signal that catches cross-platform reposts
        # once the link / canonical_link diverge, and a static
        # ``created_at`` window blunts it for long-tail roles.
        #
        # Phase B widens the in-window predicate to OR both fingerprint
        # columns: ``content_fingerprint`` (case+whitespace fold) and
        # ``normalized_fingerprint`` (slug fold — unicode dashes, smart
        # quotes, punctuation). Either-or so an old row written before
        # the new column was populated still matches by the legacy
        # signal, and a punctuation-drift twin (the JP 1329 / 3323
        # en-dash vs hyphen pair) collapses by the new one.
        fp_predicate = Q()
        if post.content_fingerprint:
            fp_predicate |= Q(content_fingerprint=post.content_fingerprint)
        if post.normalized_fingerprint:
            fp_predicate |= Q(normalized_fingerprint=post.normalized_fingerprint)
        hit = (
            JobPost.objects
            .filter(fp_predicate, last_seen_at__gte=cutoff)
            .exclude(pk=post.pk)
            .order_by("created_at")
            .first()
        )
        if hit:
            return hit.canonical

    return None


def bump_last_seen(post, *, now=None) -> None:
    """Set ``post.last_seen_at`` to ``now()`` and persist it.

    Called from every write path that resolves an incoming JobPost
    shell to an existing row — see the call sites in
    ``views/jobs.py:create()``,
    ``lib/parsers/job_post_extractor.py`` (link-hit + stub-upgrade
    branches), ``lib/job_post_merge.py``, ``lib/federation_ingest.py``,
    and ``models/job_post.from_json``. Bumping rolls the fingerprint
    window forward so a long-tail role keeps being dedupe-eligible
    while it keeps being re-seen.

    Uses a targeted ``update_fields`` save so concurrent writes that
    touch unrelated columns don't get clobbered. Idempotent — calling
    twice in a single request is cheap (two UPDATEs hitting one
    column) but pointless; callers should call once per dedupe
    decision, not per field merge.

    ``now`` is injected for tests; production callers should omit it
    and let the helper pull ``timezone.now()`` itself.
    """
    from django.utils import timezone as _tz

    if post is None or not getattr(post, "pk", None):
        return
    post.last_seen_at = now or _tz.now()
    post.save(update_fields=["last_seen_at"])
