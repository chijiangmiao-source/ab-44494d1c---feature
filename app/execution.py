"""Resumable execution check-off for audited inspection routes.

An execution session is created from one successful audit and stays bound to
that audit's route summary (canonical vector + ordered route steps) and to
the depot (检修口).  The inspector confirms the walk step by step, submitting
edge id + direction + copy number; the backend only ever advances a
contiguous prefix, and only when the submission is exactly the next expected
route step.

Idempotency, cursors, atomicity
-------------------------------
Both starting and stepping carry a client-supplied unique printable-ASCII
operation id.  The first successful result is persisted as a receipt: a
retry with the same id and identical content returns the stored first
result, while reusing the id with different content is rejected.  Steps also
carry the cursor the client based its submission on, so stale cursors and
competing tabs are rejected deterministically.  Cursor advance and receipt
insert commit in a single SQLite transaction, hence a failure never
partially advances.  Sessions (cursor) and receipts live in a SQLite
database file, so a web-process restart does not lose the confirmed prefix,
and a session never re-binds to a different audit conclusion.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .solver import AuditError, audit

OP_ID_RE = re.compile(r"^[!-~]{1,64}$")  # printable non-space ASCII, bounded

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id   TEXT PRIMARY KEY,
  audit_id     TEXT NOT NULL,
  start_node   TEXT NOT NULL,
  summary_json TEXT NOT NULL,
  route_json   TEXT NOT NULL,
  total_steps  INTEGER NOT NULL,
  cursor       INTEGER NOT NULL DEFAULT 0,
  status       TEXT NOT NULL DEFAULT 'active',
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
  op_id         TEXT PRIMARY KEY,
  session_id    TEXT NOT NULL,
  kind          TEXT NOT NULL,
  request_hash  TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at    TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(obj: Any) -> str:
    return hashlib.sha256(_canonical_json(obj).encode("utf-8")).hexdigest()


def audit_fingerprint(result) -> str:
    """Fingerprint of the audit conclusion an execution session binds to."""
    summary = {
        "nodes": list(result.nodes),
        "edges": [[e.eid, e.u, e.v, e.length] for e in result.edges],
        "start": result.start,
        "canonicalVector": result.bit_vector,
        "route": [[s.edge_id, s.frm, s.to, s.duplicate_no] for s in result.route],
    }
    return _hash(summary)


def route_steps_json(result) -> list:
    return [
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


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class ExecutionStore:
    """SQLite-backed persistence for execution sessions and op receipts.

    A single ``BEGIN IMMEDIATE`` transaction serializes every
    read-modify-write across threads and across gunicorn worker processes,
    which is what makes concurrent tabs / retries resolve deterministically.
    """

    def __init__(self, path: str):
        if path == ":memory:":
            raise ValueError("执行核对需要可持久化的数据库文件路径")
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        """Serialized read-modify-write; commit only when the body succeeds."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                conn.close()
                raise
            try:
                conn.execute("COMMIT")
            finally:
                conn.close()

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


def _err(code: str, message: str, **extra) -> Dict[str, Any]:
    body = {"ok": False, "code": code, "error": message}
    body.update(extra)
    return body


def _state_from_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    route = json.loads(rec["route_json"])
    summary = json.loads(rec["summary_json"])
    cursor = rec["cursor"]
    total = rec["total_steps"]
    return {
        "ok": True,
        "sessionId": rec["session_id"],
        "auditId": rec["audit_id"],
        "status": rec["status"],
        "start": rec["start_node"],
        "cursor": cursor,
        "totalSteps": total,
        "nextStep": route[cursor] if cursor < total else None,
        "route": route,
        "canonicalVector": summary["canonicalVector"],
        "totalLength": summary["totalLength"],
        "addedLength": summary["addedLength"],
        "optimalCount": summary["optimalCount"],
        "returnedToDepot": bool(route)
        and cursor >= total
        and route[-1]["to"] == rec["start_node"],
        "createdAt": rec["created_at"],
        "updatedAt": rec["updated_at"],
    }


def _state_fields(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Current session state merged into an error response (for resync)."""
    state = _state_from_record(rec)
    state.pop("ok", None)
    return state


def _find_receipt(conn, op_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM receipts WHERE op_id = ?", (op_id,)
    ).fetchone()


def _replay_or_conflict(receipt, request_hash: str) -> Optional[Dict[str, Any]]:
    """Return the stored first result, or a stable rejection; None if fresh."""
    if receipt is None:
        return None
    if receipt["request_hash"] != request_hash:
        return _err(
            "op_conflict",
            "操作标识已被不同内容的请求占用，已稳定拒绝；请换用新的操作标识",
        )
    stored = json.loads(receipt["response_json"])
    stored["replayed"] = True
    return stored


def _check_op_id(op_id: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(op_id, str) or not OP_ID_RE.match(op_id):
        return _err(
            "bad_op_id",
            "必须携带唯一的可打印 ASCII 操作标识（1–64 个非空白字符）",
        )
    return None


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------


def start_execution(store: ExecutionStore, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Create a session bound to the audit conclusion of ``payload['audit']``."""
    bad = _check_op_id(payload.get("opId"))
    if bad:
        return bad
    op_id = payload["opId"]

    audit_payload = payload.get("audit")
    if not isinstance(audit_payload, dict):
        return _err("bad_request", "缺少审计输入 audit（nodes/edges/start）")
    nodes = audit_payload.get("nodes", [])
    edges = audit_payload.get("edges", [])
    start = audit_payload.get("start")
    if not isinstance(nodes, list):
        nodes = []
    if not isinstance(edges, list):
        edges = []
    try:
        result = audit(nodes, edges, start)
    except AuditError as exc:
        return {
            "ok": False,
            "code": "audit_invalid",
            "error": exc.message,
            "fields": list(exc.fields),
            "locations": exc.locations,
        }

    request_hash = _hash({"opId": op_id, "audit": audit_payload})
    now = _now()
    with store.transaction() as conn:
        prior = _replay_or_conflict(_find_receipt(conn, op_id), request_hash)
        if prior is not None:
            return prior

        session_id = "s" + secrets.token_hex(16)
        route = route_steps_json(result)
        summary = {
            "canonicalVector": result.bit_vector,
            "totalLength": result.total_length,
            "addedLength": result.added_length,
            "optimalCount": result.optimal_count,
        }
        rec = {
            "session_id": session_id,
            "audit_id": audit_fingerprint(result),
            "start_node": result.start,
            "summary_json": _canonical_json(summary),
            "route_json": _canonical_json(route),
            "total_steps": len(route),
            "cursor": 0,
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }
        conn.execute(
            "INSERT INTO sessions (session_id, audit_id, start_node, summary_json,"
            " route_json, total_steps, cursor, status, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                rec["session_id"],
                rec["audit_id"],
                rec["start_node"],
                rec["summary_json"],
                rec["route_json"],
                rec["total_steps"],
                rec["cursor"],
                rec["status"],
                rec["created_at"],
                rec["updated_at"],
            ),
        )
        state = _state_from_record(rec)
        conn.execute(
            "INSERT INTO receipts (op_id, session_id, kind, request_hash,"
            " response_json, created_at) VALUES (?,?,?,?,?,?)",
            (op_id, session_id, "start", request_hash, _canonical_json(state), now),
        )
        response = dict(state)
        response["replayed"] = False
        return response


# ---------------------------------------------------------------------------
# Step
# ---------------------------------------------------------------------------


def _normalize_step(step: Any):
    if not isinstance(step, dict):
        return None, _err("bad_request", "缺少步骤内容 step（edgeId/from/to/copy）")
    edge_id, frm, to = step.get("edgeId"), step.get("from"), step.get("to")
    if not all(isinstance(x, str) and x for x in (edge_id, frm, to)):
        return None, _err("bad_request", "步骤需包含非空的 edgeId / from / to")
    copy = step.get("copy")
    if isinstance(copy, bool) or not isinstance(copy, int) or copy < 1:
        return None, _err("bad_request", "副本号 copy 必须为 ≥1 的整数")
    return {"edgeId": edge_id, "from": frm, "to": to, "copy": copy}, None


def advance_execution(store: ExecutionStore, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Advance the confirmed prefix by exactly one matching step."""
    bad = _check_op_id(payload.get("opId"))
    if bad:
        return bad
    op_id = payload["opId"]

    session_id = payload.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        return _err("bad_request", "缺少会话标识 sessionId")
    expected = payload.get("expectedCursor")
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
        return _err("bad_request", "expectedCursor 必须为非负整数")
    step, bad = _normalize_step(payload.get("step"))
    if bad:
        return bad

    request_hash = _hash(
        {
            "opId": op_id,
            "sessionId": session_id,
            "expectedCursor": expected,
            "step": step,
        }
    )
    now = _now()
    with store.transaction() as conn:
        prior = _replay_or_conflict(_find_receipt(conn, op_id), request_hash)
        if prior is not None:
            return prior

        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return _err("not_found", "执行会话不存在或已被清理")
        rec = dict(row)
        route = json.loads(rec["route_json"])
        cursor = rec["cursor"]
        total = rec["total_steps"]

        if rec["status"] != "active":
            return _err(
                "completed",
                "该执行会话已全部核对完成，不能继续推进",
                **_state_fields(rec),
            )
        if expected != cursor:
            return _err(
                "stale_cursor",
                f"游标已过期：服务端已核对到第 {cursor} 步，请按当前状态重试",
                **_state_fields(rec),
            )
        want = route[cursor]
        if not (
            step["edgeId"] == want["edgeId"]
            and step["from"] == want["from"]
            and step["to"] == want["to"]
            and step["copy"] == want["copy"]
        ):
            return _err(
                "step_mismatch",
                "与下一预期步骤不一致：第 {} 步应为 {} {}→{} 第{}副本".format(
                    cursor + 1, want["edgeId"], want["from"], want["to"], want["copy"]
                ),
                expectedStep=want,
                **_state_fields(rec),
            )

        new_cursor = cursor + 1
        rec["cursor"] = new_cursor
        rec["status"] = "completed" if new_cursor >= total else "active"
        rec["updated_at"] = now
        conn.execute(
            "UPDATE sessions SET cursor = ?, status = ?, updated_at = ?"
            " WHERE session_id = ?",
            (rec["cursor"], rec["status"], rec["updated_at"], session_id),
        )
        state = _state_from_record(rec)
        state["confirmedStep"] = want
        conn.execute(
            "INSERT INTO receipts (op_id, session_id, kind, request_hash,"
            " response_json, created_at) VALUES (?,?,?,?,?,?)",
            (op_id, session_id, "step", request_hash, _canonical_json(state), now),
        )
        response = dict(state)
        response["replayed"] = False
        return response


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def get_execution(store: ExecutionStore, session_id: str) -> Dict[str, Any]:
    rec = store.get_session(session_id)
    if rec is None:
        return _err("not_found", "执行会话不存在或已被清理")
    return _state_from_record(rec)
