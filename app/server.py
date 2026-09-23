"""HTTP API for the pipe-network route audit and on-site execution."""

from __future__ import annotations

import os
import re
from typing import Any, Dict

from . import execution as exec_mod
from .execution import ExecutionError, ExecutionStore
from .solver import AuditError, Edge, audit

OP_ID_RE = re.compile(r"^[!-~]{1,64}$")
TOKEN_RE = re.compile(r"^[!-~]+$")


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


# ---------------------------------------------------------------------------
# Execution request validation
# ---------------------------------------------------------------------------


def _bad_exec(message: str, code: str = "bad_request") -> dict:
    return {"ok": False, "error": message, "code": code}


def _as_op_id(value) -> str:
    if not isinstance(value, str) or not OP_ID_RE.match(value):
        raise ExecutionError(
            "操作标识必须为 1–64 个非空白 ASCII 字符", "bad_op_id", 400)
    return value


def _as_token(value, what: str) -> str:
    if not isinstance(value, str) or not TOKEN_RE.match(value):
        raise ExecutionError(f"{what}必须为非空白 ASCII 字符串",
                             "bad_request", 400)
    return value


def _as_copy(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ExecutionError(
            "副本号必须为不小于 1 的整数", "bad_request", 400)
    return value


def create_app(store: ExecutionStore | None = None):
    from flask import Flask, jsonify, request, send_from_directory

    def get_store() -> ExecutionStore:
        return store if store is not None else exec_mod.get_store()

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

    # -- resumable on-site execution --------------------------------------

    @app.post("/api/execute/start")
    def api_execute_start():
        """Bind a new session to one successful audit conclusion."""
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(_bad_exec("请求体必须为 JSON 对象")), 400
        try:
            op_id = _as_op_id(payload.get("opId"))
        except ExecutionError as exc:
            return jsonify(exc.body), exc.http_status

        audit_payload = payload.get("audit")
        if not isinstance(audit_payload, dict):
            audit_payload = {}
        result = run_audit(audit_payload)

        st = get_store()
        # A failed audit never creates a session; record a receipt so the
        # op id cannot be retried with different content.
        if not result.get("ok"):
            content = exec_mod.start_content_hash(audit_payload)
            try:
                status, body = st.record_failure(op_id, content, result)
            except ExecutionError as exc:
                return jsonify(exc.body), exc.http_status
            return jsonify(body), status

        try:
            status, body = st.start(
                op_id=op_id,
                raw_payload=audit_payload,
                start_node=result["start"],
                steps=result["route"],
                edge_orient=result["edges"],
                result_body=result,
            )
        except ExecutionError as exc:
            return jsonify(exc.body), exc.http_status
        return jsonify(body), status

    @app.post("/api/execute/advance")
    def api_execute_advance():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(_bad_exec("请求体必须为 JSON 对象")), 400
        try:
            op_id = _as_op_id(payload.get("opId"))
            session_id = _as_token(payload.get("sessionId"), "会话标识")
            cursor = _as_token(payload.get("cursor"), "执行游标")
            edge_id = _as_token(payload.get("edgeId"), "管段标识")
            if payload.get("direction") not in ("forward", "reverse"):
                raise ExecutionError(
                    "方向必须为 forward 或 reverse", "bad_request", 400)
            direction = payload["direction"]
            copy_no = _as_copy(payload.get("copy"))
        except ExecutionError as exc:
            return jsonify(exc.body), exc.http_status

        try:
            status, body = get_store().advance(
                session_id=session_id,
                cursor=cursor,
                op_id=op_id,
                edge_id=edge_id,
                direction=direction,
                copy_no=copy_no,
            )
        except ExecutionError as exc:
            return jsonify(exc.body), exc.http_status
        return jsonify(body), status

    @app.get("/api/execute/status")
    def api_execute_status():
        session_id = request.args.get("sessionId", "")
        if not TOKEN_RE.match(session_id):
            return jsonify(_bad_exec("会话标识无效")), 400
        try:
            body = get_store().status(session_id)
        except ExecutionError as exc:
            return jsonify(exc.body), exc.http_status
        return jsonify(body)

    return app


app = create_app()
