"""Scrape-graph read endpoints for the d3 introspection UI.

Three surfaces:
- GET /api/v1/admin/graph-structure/ — static node/edge registry for
  the "architecture" view.
- GET /api/v1/admin/graph-aggregate/?since=DATE — per-edge counts +
  success rates, the eval-loop view.
- GET /api/v1/scrapes/:id/graph-trace/ — lives on ScrapeViewSet (see
  scrapes.py::graph_trace). Kept there so per-scrape auth stays with
  the scrape detail permissions.

The canonical graph definition lives in ai/lib/scrape_graph/ (ai/
owns the runtime). api/ ships a static snapshot for UI rendering; if
they drift, re-export by running `ai/scripts/export_graph_structure.py`
(to be added with the ai PR).
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response


# Snapshot of the scrape-graph node/edge shape. Sync with
# ai/lib/scrape_graph/graph.py when nodes change.
_NODES = [
    # Scrape-side
    {"id": "StartScrape", "group": "scrape", "label": "Start"},
    {"id": "LoadProfile", "group": "scrape", "label": "Load profile"},
    {"id": "Navigate", "group": "scrape", "label": "Navigate"},
    {"id": "ResolveFinalUrl", "group": "scrape", "label": "Resolve final URL"},
    {"id": "CheckLinkDedup", "group": "scrape", "label": "Check link dedup"},
    {"id": "DuplicateShortCircuit", "group": "terminal", "label": "Duplicate short-circuit"},
    {"id": "WaitReadySelector", "group": "scrape", "label": "Wait ready selector"},
    {"id": "SettleWait", "group": "scrape", "label": "Settle wait"},
    {"id": "ExpandTruncations", "group": "scrape", "label": "Expand truncations"},
    {"id": "DetectObstacle", "group": "obstacle", "label": "Detect obstacle"},
    {"id": "ObstacleRememberMe", "group": "obstacle", "label": "Remember-me reauth"},
    {"id": "ObstacleWaitRetry", "group": "obstacle", "label": "Wait + retry"},
    {"id": "ObstacleAgent", "group": "obstacle", "label": "Obstacle agent"},
    {"id": "ObstacleFail", "group": "terminal", "label": "Obstacle fail"},
    {"id": "Capture", "group": "scrape", "label": "Capture"},
    {"id": "PersistScrape", "group": "scrape", "label": "Persist scrape"},
    # Extract-side
    {"id": "StartExtract", "group": "extract", "label": "Start extract"},
    {"id": "Tier0CSS", "group": "extract", "label": "Tier 0 CSS"},
    {"id": "Tier1Mini", "group": "extract", "label": "Tier 1 mini"},
    {"id": "Tier2Haiku", "group": "extract", "label": "Tier 2 haiku"},
    {"id": "Tier3Sonnet", "group": "extract", "label": "Tier 3 sonnet"},
    {"id": "EvaluateExtraction", "group": "extract", "label": "Evaluate extraction"},
    {"id": "PersistJobPost", "group": "extract", "label": "Persist job post"},
    {"id": "UpdateProfile", "group": "extract", "label": "Update profile"},
    {"id": "ResolveApplyUrl", "group": "extract", "label": "Resolve apply URL"},
    {"id": "ExtractFail", "group": "terminal", "label": "Extract fail"},
]

_EDGES = [
    ("StartScrape", "LoadProfile"),
    ("LoadProfile", "Navigate"),
    ("Navigate", "ResolveFinalUrl"),
    ("ResolveFinalUrl", "CheckLinkDedup"),
    ("CheckLinkDedup", "DuplicateShortCircuit"),
    ("CheckLinkDedup", "WaitReadySelector"),
    ("WaitReadySelector", "ExpandTruncations"),
    ("WaitReadySelector", "SettleWait"),
    ("SettleWait", "ExpandTruncations"),
    ("ExpandTruncations", "DetectObstacle"),
    ("DetectObstacle", "ObstacleRememberMe"),
    ("DetectObstacle", "ObstacleWaitRetry"),
    ("DetectObstacle", "ObstacleAgent"),
    ("DetectObstacle", "Capture"),
    ("DetectObstacle", "ObstacleFail"),
    ("ObstacleRememberMe", "DetectObstacle"),
    ("ObstacleRememberMe", "ObstacleWaitRetry"),
    ("ObstacleWaitRetry", "DetectObstacle"),
    ("ObstacleWaitRetry", "ObstacleAgent"),
    ("ObstacleAgent", "DetectObstacle"),
    ("ObstacleAgent", "ObstacleFail"),
    ("Capture", "PersistScrape"),
    ("PersistScrape", "StartExtract"),
    ("StartExtract", "Tier0CSS"),
    ("Tier0CSS", "EvaluateExtraction"),
    ("Tier0CSS", "Tier1Mini"),
    ("Tier0CSS", "Tier2Haiku"),
    ("Tier1Mini", "EvaluateExtraction"),
    ("Tier2Haiku", "EvaluateExtraction"),
    ("Tier3Sonnet", "EvaluateExtraction"),
    ("EvaluateExtraction", "PersistJobPost"),
    ("EvaluateExtraction", "Tier1Mini"),
    ("EvaluateExtraction", "Tier2Haiku"),
    ("EvaluateExtraction", "Tier3Sonnet"),
    ("EvaluateExtraction", "ExtractFail"),
    ("PersistJobPost", "UpdateProfile"),
    ("UpdateProfile", "ResolveApplyUrl"),
]


@api_view(["GET"])
@permission_classes([IsAdminUser])
def graph_structure(request):
    """Static node + edge registry for the scrape-graph architecture view."""
    edges = [{"from": a, "to": b} for (a, b) in _EDGES]
    return Response({"data": {"nodes": _NODES, "edges": edges}})


@api_view(["GET"])
@permission_classes([IsAdminUser])
def graph_mermaid(request):
    """Emit a mermaid stateDiagram of the scrape-graph.

    Renders cleanly in the frontend via mermaid.js, on the GitHub
    README, or by pasting into https://mermaid.live — the shape
    matches pydantic-graph's Graph.mermaid_code() output so the
    same diagram can be regenerated once the ai-side Graph
    registration lands in Phase 1d.

    Optional query param:
    - ?as=text  → return text/plain (default is application/json). Not
      `format` because DRF reserves that for content negotiation.
    """
    lines = ["stateDiagram-v2"]
    # Styled groups via class-def so d3 / mermaid users can theme them.
    lines.append("    classDef scrape fill:#dbeafe,stroke:#3b82f6")
    lines.append("    classDef obstacle fill:#fee2e2,stroke:#ef4444")
    lines.append("    classDef extract fill:#dcfce7,stroke:#22c55e")
    lines.append("    classDef terminal fill:#e5e7eb,stroke:#6b7280")

    entry_id = "StartScrape"
    lines.append(f"    [*] --> {entry_id}")
    for (a, b) in _EDGES:
        lines.append(f"    {a} --> {b}")

    # Terminal → End markers
    for terminal_id in ("DuplicateShortCircuit", "ObstacleFail", "ResolveApplyUrl", "ExtractFail"):
        lines.append(f"    {terminal_id} --> [*]")

    # Group class assignments
    group_nodes = {}
    for node in _NODES:
        group_nodes.setdefault(node["group"], []).append(node["id"])
    for group, ids in group_nodes.items():
        lines.append(f"    class {','.join(ids)} {group}")

    mermaid = "\n".join(lines)

    if request.query_params.get("as") == "text":
        from django.http import HttpResponse
        return HttpResponse(mermaid, content_type="text/plain")
    return Response({"data": {"mermaid": mermaid}})


@api_view(["GET"])
@permission_classes([IsAdminUser])
def graph_aggregate(request):
    """Per-edge counts + success rates across recent scrape runs.

    Query params:
    - since: ISO date OR shorthand like "7d", "30d" (default 7d).
    """
    from job_hunting.models.scrape_status import ScrapeStatus

    since_raw = request.query_params.get("since", "7d")
    cutoff = _parse_since(since_raw)

    # Terminal outcome per scrape (last ScrapeStatus with a terminal graph_node).
    terminal_nodes = {"DuplicateShortCircuit", "ObstacleFail", "ExtractFail", "ResolveApplyUrl"}
    scrape_outcomes: dict[int, str] = {}
    terminal_rows = (
        ScrapeStatus.objects
        .filter(graph_node__in=terminal_nodes, created_at__gte=cutoff)
        .order_by("scrape_id", "-created_at")
        .values("scrape_id", "graph_node")
    )
    for row in terminal_rows:
        scrape_outcomes.setdefault(row["scrape_id"], row["graph_node"])

    # Edge counts — each ScrapeStatus records `routed_to` in payload.
    edge_agg: dict[tuple[str, str], dict] = {}
    rows = (
        ScrapeStatus.objects
        .filter(graph_node__isnull=False, created_at__gte=cutoff)
        .values("scrape_id", "graph_node", "graph_payload")
    )
    for row in rows:
        routed_to = (row.get("graph_payload") or {}).get("routed_to")
        if not routed_to:
            continue
        key = (row["graph_node"], routed_to)
        agg = edge_agg.setdefault(key, {"count": 0, "success_count": 0})
        agg["count"] += 1
        terminal = scrape_outcomes.get(row["scrape_id"])
        if terminal in {"DuplicateShortCircuit", "ResolveApplyUrl"}:
            agg["success_count"] += 1

    data = [
        {
            "from": frm,
            "to": to,
            "count": agg["count"],
            "success_count": agg["success_count"],
            "success_rate": (
                agg["success_count"] / agg["count"] if agg["count"] else 0.0
            ),
        }
        for (frm, to), agg in edge_agg.items()
    ]
    # Total distinct scrapes that logged any transition in the window
    total_scrapes = len({row["scrape_id"] for row in rows})
    return Response({
        "data": {"edges": data},
        "meta": {"since": cutoff.isoformat(), "total_scrapes": total_scrapes},
    })


def _parse_since(raw: str):
    raw = (raw or "").strip()
    if raw.endswith("d") and raw[:-1].isdigit():
        days = int(raw[:-1])
        return timezone.now() - timedelta(days=days)
    if raw.endswith("h") and raw[:-1].isdigit():
        return timezone.now() - timedelta(hours=int(raw[:-1]))
    # Fallback: ISO date string
    try:
        from django.utils.dateparse import parse_datetime, parse_date
        dt = parse_datetime(raw) or parse_date(raw)
        if dt is not None:
            if hasattr(dt, "hour"):
                return dt if dt.tzinfo else timezone.make_aware(dt)
            return timezone.make_aware(
                timezone.datetime.combine(dt, timezone.datetime.min.time())
            )
    except Exception:
        pass
    return timezone.now() - timedelta(days=7)
