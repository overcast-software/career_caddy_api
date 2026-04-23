"""Scrape-graph read endpoints for the d3 introspection UI.

Three surfaces:
- GET /api/v1/admin/graph-structure/ — static node/edge registry for
  the "architecture" view.
- GET /api/v1/admin/graph-aggregate/?since=DATE — per-edge counts +
  success rates, the eval-loop view.
- GET /api/v1/scrapes/:id/graph-trace/ — lives on ScrapeViewSet (see
  scrapes.py::graph_trace). Kept there so per-scrape auth stays with
  the scrape detail permissions.

The canonical graph definition lives in ai/lib/scrape_graph/graph.py
(ai/ owns the runtime). api/ reads a committed snapshot from
graph_static.json next to this file; regenerate it with
`uv run caddy-export-graph` in ai/ after changing node topology. A
drift test in ai/tests/ guards against stale snapshots.
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response


_SNAPSHOT_PATH = Path(__file__).with_name("graph_static.json")
with _SNAPSHOT_PATH.open() as _f:
    _SNAPSHOT = json.load(_f)

_NODES = _SNAPSHOT["nodes"]
_EDGES = [(e["from"], e["to"]) for e in _SNAPSHOT["edges"]]


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
