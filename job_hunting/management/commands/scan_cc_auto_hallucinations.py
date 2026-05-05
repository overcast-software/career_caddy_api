"""Read-only triage scan for cc_auto-sourced JobPosts whose stored
title may not match what the destination URL actually points at.

Why this exists: cc_auto parses multi-job email digests into JobPosts.
Across-row alignment in the LLM extractor can drift, leaving
(link, title, company) triples where the link goes to a different job
than the title says (the jp/1724 case — stored "Junior FSD at Web
Connectivity LLC" pointed at SNBL bilingual BD/PM on ZipRecruiter).

The scan fetches each link, parses ``<title>``, computes Jaccard +
SequenceMatcher similarity against the stored title, and surfaces rows
where similarity is low enough to warrant human review.

Read-only — no model writes. Output is a markdown table on stdout plus
an optional JSON sidecar via ``--json``.

Caveats:
- Many job sites gate plain HTTP fetches behind bot detection. We
  classify those as ``http_error`` / ``fetch_error`` rather than
  pretending we got a real title — false negatives are expected and
  acceptable for a triage starting point.
- Polite delay defaults to 1s between requests; raise it for hosts you
  don't own.
"""

from __future__ import annotations

import json
import time
from difflib import SequenceMatcher

import httpx
from bs4 import BeautifulSoup
from django.core.management.base import BaseCommand
from django.db.models import Q

from job_hunting.models import JobPost


CC_AUTO_SOURCES = ("email", "email_direct")
USER_AGENT = (
    "CareerCaddy-HallucinationScan/1.0 "
    "(+https://careercaddy.online; ops triage scan)"
)


def _tokens(s: str) -> set[str]:
    return {t for t in (s or "").lower().split() if len(t) > 2}


def jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def fetch_page_title(
    url: str, *, timeout: float, client: httpx.Client | None = None
) -> tuple[str, str | None, int | None]:
    """Return (status_label, title_text_or_None, http_status_or_None).

    status_label ∈ {"ok", "http_error", "fetch_error", "parse_error"}.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
    }
    if client is None:
        owned = httpx.Client(
            follow_redirects=True, timeout=timeout, headers=headers
        )
        try:
            return fetch_page_title(url, timeout=timeout, client=owned)
        finally:
            owned.close()

    try:
        r = client.get(url)
    except httpx.HTTPError as e:
        return ("fetch_error", str(e)[:120], None)

    if r.status_code >= 400:
        return ("http_error", None, r.status_code)

    try:
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:  # pragma: no cover — bs4 is permissive
        return ("parse_error", str(e)[:120], r.status_code)

    title_tag = soup.find("title")
    title_text = title_tag.get_text(strip=True) if title_tag else ""
    return ("ok", title_text or None, r.status_code)


# Substrings that signal we're looking at an auth wall / bot challenge
# rather than the real job page. LinkedIn's /comm/ links 100% land here
# without a session cookie; Indeed and Glassdoor sit behind Cloudflare
# JS challenges. Treating these as "mismatch" would drown the report in
# false positives — they're "indeterminate" by definition.
AUTH_WALL_MARKERS = (
    "sign in",
    "log in",
    "login",
    "just a moment",
    "access denied",
    "verify you are human",
    "are you a robot",
    "captcha",
    "attention required",
)


def is_auth_wall(page_title: str) -> bool:
    lowered = page_title.lower()
    return any(m in lowered for m in AUTH_WALL_MARKERS)


def classify(stored_title: str, page_title: str | None, threshold: float) -> str:
    """Return one of: match, mismatch, indeterminate."""
    if not page_title:
        return "indeterminate"
    if is_auth_wall(page_title):
        return "indeterminate"
    j = jaccard(stored_title, page_title)
    r = ratio(stored_title, page_title)
    # Either signal landing above threshold counts as "match" — token
    # overlap catches reorderings, sequence ratio catches punctuation
    # variants.
    if j >= threshold or r >= threshold + 0.2:
        return "match"
    return "mismatch"


class Command(BaseCommand):
    help = "Scan cc_auto-sourced JobPosts for stored-title vs page-title drift."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=200,
            help="Max rows to scan, recent-first (default 200).",
        )
        parser.add_argument(
            "--source", action="append", default=None,
            help=(
                "Source values to include (repeatable). "
                f"Default: {list(CC_AUTO_SOURCES)}."
            ),
        )
        parser.add_argument(
            "--timeout", type=float, default=10.0,
            help="Per-request HTTP timeout seconds (default 10).",
        )
        parser.add_argument(
            "--delay", type=float, default=1.0,
            help="Sleep seconds between requests (default 1).",
        )
        parser.add_argument(
            "--threshold", type=float, default=0.3,
            help="Jaccard threshold below which a row is flagged (default 0.3).",
        )
        parser.add_argument(
            "--include-thin-only", action="store_true",
            help="Only scan posts with thin description (<200 chars or empty).",
        )
        parser.add_argument(
            "--json", dest="json_out", default=None,
            help="Write full per-row results to this JSON path.",
        )

    def handle(self, *args, **opts):
        sources = tuple(opts["source"] or CC_AUTO_SOURCES)
        qs = (
            JobPost.objects
            .filter(source__in=sources, link__isnull=False)
            .exclude(link="")
            .order_by("-created_at")
        )
        if opts["include_thin_only"]:
            qs = qs.filter(Q(description__isnull=True) | Q(description=""))
        rows = list(qs[: opts["limit"]])

        results: list[dict] = []
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        }
        with httpx.Client(
            follow_redirects=True, timeout=opts["timeout"], headers=headers
        ) as client:
            for jp in rows:
                status, page_title, http_status = fetch_page_title(
                    jp.link, timeout=opts["timeout"], client=client
                )
                verdict = (
                    classify(jp.title or "", page_title, opts["threshold"])
                    if status == "ok"
                    else "indeterminate"
                )
                results.append({
                    "id": jp.id,
                    "stored_title": jp.title,
                    "stored_company": jp.company.name if jp.company_id else None,
                    "link": jp.link,
                    "fetch_status": status,
                    "http_status": http_status,
                    "page_title": page_title,
                    "jaccard": round(jaccard(jp.title or "", page_title or ""), 3),
                    "ratio": round(ratio(jp.title or "", page_title or ""), 3),
                    "verdict": verdict,
                })
                if opts["delay"]:
                    time.sleep(opts["delay"])

        self._render_table(results)

        if opts["json_out"]:
            with open(opts["json_out"], "w") as f:
                json.dump(results, f, indent=2, default=str)
            self.stdout.write(f"\nWrote {len(results)} rows to {opts['json_out']}")

    def _render_table(self, results: list[dict]) -> None:
        flagged = [r for r in results if r["verdict"] == "mismatch"]
        indeterminate = [r for r in results if r["verdict"] == "indeterminate"]
        matched = [r for r in results if r["verdict"] == "match"]

        self.stdout.write(
            f"\nScanned {len(results)} rows  "
            f"|  match={len(matched)}  "
            f"mismatch={len(flagged)}  "
            f"indeterminate={len(indeterminate)}\n"
        )

        if flagged:
            self.stdout.write("\n## Mismatches (flagged for review)\n")
            self.stdout.write(
                "| id | stored title | page title | jaccard | ratio | link |"
            )
            self.stdout.write("|---|---|---|---|---|---|")
            for r in flagged:
                self.stdout.write(
                    f"| {r['id']} "
                    f"| {(r['stored_title'] or '')[:60]} "
                    f"| {(r['page_title'] or '')[:60]} "
                    f"| {r['jaccard']} "
                    f"| {r['ratio']} "
                    f"| {r['link'][:60]} |"
                )

        if indeterminate:
            self.stdout.write("\n## Indeterminate (could not fetch / no title)\n")
            self.stdout.write("| id | fetch_status | http | stored title | link |")
            self.stdout.write("|---|---|---|---|---|")
            for r in indeterminate:
                self.stdout.write(
                    f"| {r['id']} "
                    f"| {r['fetch_status']} "
                    f"| {r['http_status'] or '-'} "
                    f"| {(r['stored_title'] or '')[:60]} "
                    f"| {r['link'][:60]} |"
                )
