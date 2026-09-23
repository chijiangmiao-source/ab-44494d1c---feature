"""HTTP API for the pipe-network route audit."""

from __future__ import annotations

import os
import threading
from typing import Any, Dict

from .execution import (
    ExecutionStore,
    advance_execution,
    audit_fingerprint,
    get_execution,
    start_execution,
)
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
        "auditId": audit_fingerprint(result),
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


def _default_db_path() -> str:
    return os.environ.get("PIPE_AUDIT_DB") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data",
        "execution.db",
    )


def create_app(store: ExecutionStore | None = None):
    from flask import Flask, jsonify, request, send_from_directory

    static_dir = os.path.join(os.path.dirname(__file__), "static")
    app = Flask(__name__, static_folder=static_dir, static_url_path="")

    # Lazily created so importing this module (e.g. for run_audit) has no
    # filesystem side effects; one store per worker process, all sharing the
    # same SQLite file.
    _store_holder: Dict[str, Any] = {"store": store}
    _store_lock = threading.Lock()

    def exec_store() -> ExecutionStore:
        with _store_lock:
            if _store_holder["store"] is None:
                _store_holder["store"] = ExecutionStore(_default_db_path())
            return _store_holder["store"]

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

    @app.post("/api/execution/start")
    def api_execution_start():
        payload = request.get_json(silent=True) or {}
        return jsonify(start_execution(exec_store(), payload))

    @app.post("/api/execution/step")
    def api_execution_step():
        payload = request.get_json(silent=True) or {}
        return jsonify(advance_execution(exec_store(), payload))

    @app.get("/api/execution/<session_id>")
    def api_execution_get(session_id):
        return jsonify(get_execution(exec_store(), session_id))

    return app


app = create_app()
