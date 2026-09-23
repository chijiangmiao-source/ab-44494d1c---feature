"""HTTP API for the pipe-network route audit."""

from __future__ import annotations

import os
from typing import Any, Dict

from .solver import AuditError, Edge, RouteStep, audit


def _serialize(nodes, edges: list[Edge], result) -> Dict[str, Any]:
    edge_objs = [
        {
            "id": e.eid,
            "u": e.u,
            "v": e.v,
            "length": e.length,
            "index": e.index,
            "classification": result.classification[e.index],
            "duplicated": e.index in result.canonical_set,
            "copies": result.multiplicity[e.index],
        }
        for e in edges
    ]
    route = [
        {
            "seq": i + 1,
            "edgeIndex": st.edge_index,
            "edgeId": st.edge_id,
            "from": st.frm,
            "to": st.to,
            "length": st.length,
            "copy": st.duplicate_no,
        }
        for i, st in enumerate(result.route)
    ]
    # positions at which each edge occurs in the route, for highlighting
    positions: Dict[int, list] = {i: [] for i in range(len(edges))}
    for i, st in enumerate(result.route):
        positions[st.edge_index].append(i + 1)

    return {
        "ok": True,
        "nodes": nodes,
        "edges": edge_objs,
        "start": result.start,
        "oddVertices": list(result.odd_vertices),
        "totalLength": result.total_length,
        "addedLength": result.added_length,
        "optimalCount": result.optimal_count,
        "canonicalVector": result.bit_vector,
        "canonicalEdges": [
            edges[i].eid for i in sorted(result.canonical_set)
        ],
        "route": route,
        "positions": {str(k): v for k, v in positions.items()},
        "eulerian": result.is_eulerian,
    }


def run_audit(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Pure logic entry: validate + audit, return a response dict."""
    nodes = payload.get("nodes", [])
    edges = payload.get("edges", [])
    start = payload.get("start")
    if not isinstance(nodes, list):
        nodes = []
    if not isinstance(edges, list):
        edges = []
    try:
        result = audit(nodes, edges, start)
    except AuditError as exc:
        return {
            "ok": False,
            "error": exc.message,
            "fields": list(exc.fields),
            "locations": exc.locations,
        }
    return _serialize(result.nodes, result.edges, result)


def create_app():
    from flask import Flask, jsonify, request, send_from_directory

    static_dir = os.path.join(os.path.dirname(__file__), "static")
    app = Flask(__name__, static_folder=static_dir, static_url_path="")

    @app.get("/")
    def index():
        return send_from_directory(static_dir, "index.html")

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.post("/api/audit")
    def api_audit():
        payload = request.get_json(silent=True) or {}
        return jsonify(run_audit(payload))

    return app


app = create_app()
