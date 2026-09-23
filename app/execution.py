"""Resumable, idempotent on-site execution of a canonical closed route.

An execution session is bound to exactly one successful audit: its route
fingerprint (ordered edge id / direction / copy number of every step, plus
the depot) and the depot itself.  Inspectors advance the session one step at
a time, and the backend only ever extends the *confirmed prefix*: a step is
accepted solely when it is identical to the next expected step
(edge id, orientation, copy number).  Any mismatch, stale cursor or reused
operation id is rejected without partial progress.

Guarantees
----------
* Every start / advance carries a unique ASCII operation id.  Retrying the
  same op id with the same content returns the first stored response;
  reusing an op id with different content is rejected (``op_reused``).
* Cursors are HMAC-signed and encode the session, route digest and prefix.
  A cursor that belongs to another session or whose prefix is no longer
  current (duplicate tab, skipped step, forged value) is rejected as
  ``stale_cursor`` and changes nothing.
* Sessions and receipts live in SQLite (WAL), so a web-process restart --
  including across gunicorn workers -- restores the confirmed prefix and
  keeps retries idempotent.
* A new audit produces a new session with its own fingerprint and stored
  result snapshot; old sessions never attach to new conclusions.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from typing import Any, Dict, List, Optional, Tuple


class ExecutionError(Exception):
    """Stable, non-partial rejection of an execution request."""

    def __init__(self, message: str, code: str, http_status: int = 409,
                 body: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status
        self.body = body or {"ok": False, "error": message, "code": code}


# ---------------------------------------------------------------------------
# Fingerprints / content hashing
# ---------------------------------------------------------------------------


def _canonical(obj: Any) -> bytes:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _content_hash(obj: Any) -> str:
    return hashlib.sha256(_canonical(obj)).hexdigest()


def route_fingerprint(start: str, steps: List[dict]) -> str:
    """Bind a session to one concrete route: depot + ordered traversals."""
    payload = {
        "start": start,
        "steps": [
            [st["edgeId"], st["from"], st["to"], st["copy"]] for st in steps
        ],
    }
    return hashlib.sha256(_canonical(payload)).hexdigest()


def start_content_hash(payload: dict) -> str:
    return _content_hash({"kind": "start", "payload": payload})


def advance_content_hash(
    session_id: str, cursor: str, edge_id: str, direction: str, copy_no: int
) -> str:
    return _content_hash({
        "kind": "advance",
        "sessionId": session_id,
        "cursor": cursor,
        "edgeId": edge_id,
        "direction": direction,
        "copy": copy_no,
    })


# ---------------------------------------------------------------------------
# Cursor codec (base64url(json) . base64url(hmac-sha256 truncated))
# ---------------------------------------------------------------------------


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(txt: str) -> bytes:
    pad = "=" * (-len(txt) % 4)
    return base64.urlsafe_b64decode(txt + pad)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    route_digest TEXT NOT NULL,
    start_node TEXT NOT NULL,
    total_steps INTEGER NOT NULL,
    confirmed INTEGER NOT NULL,
    steps_json TEXT NOT NULL,
    edges_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS receipts (
    op_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def default_db_path() -> str:
    env = os.environ.get("EXECUTION_DB")
    if env:
        return env
    for candidate in ("/data/executions.db",):
        try:
            os.makedirs(os.path.dirname(candidate), exist_ok=True)
            with open(candidate, "a+b"):
                pass
            return candidate
        except OSError:
            continue
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    local = os.path.join(here, "data", "executions.db")
    os.makedirs(os.path.dirname(local), exist_ok=True)
    return local


class ExecutionStore:
    """SQLite-backed session + idempotency-receipt storage."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or default_db_path()
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        self._init_lock = threading.Lock()
        self._secret = self._bootstrap()

    # -- connection plumbing ----------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=6.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _bootstrap(self) -> bytes:
        with self._init_lock:
            conn = self._connect()
            try:
                # journal_mode is outside a transaction and persisted on the
                # db file, so multiple processes cooperate in WAL mode.
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.DatabaseError:
                    pass
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.executescript(SCHEMA)
                    row = conn.execute(
                        "SELECT value FROM meta WHERE key='secret'"
                    ).fetchone()
                    if row is None:
                        secret = secrets.token_hex(32)
                        conn.execute(
                            "INSERT INTO meta(key,value) VALUES('secret',?)",
                            (secret,),
                        )
                    else:
                        secret = row["value"]
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            finally:
                conn.close()
        return secret.encode("ascii")

    # -- cursors -----------------------------------------------------------

    def encode_cursor(self, session_id: str, digest: str, prefix: int) -> str:
        body = _canonical({"s": session_id, "d": digest, "p": prefix})
        mac = hmac.new(self._secret, body, hashlib.sha256).digest()[:16]
        return _b64e(body) + "." + _b64e(mac)

    def decode_cursor(self, token: str) -> Dict[str, Any]:
        try:
            part_body, part_mac = token.split(".", 1)
            body = _b64d(part_body)
            mac = _b64d(part_mac)
        except (ValueError, TypeError):
            raise ValueError("malformed cursor")
        expect = hmac.new(self._secret, body, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(mac, expect):
            raise ValueError("bad cursor signature")
        try:
            data = json.loads(body.decode("utf-8"))
            sid, digest, prefix = data["s"], data["d"], data["p"]
        except (ValueError, KeyError, UnicodeDecodeError):
            raise ValueError("malformed cursor payload")
        if not isinstance(prefix, int) or isinstance(prefix, bool) or prefix < 0:
            raise ValueError("bad cursor prefix")
        return {"s": sid, "d": digest, "p": prefix}

    # -- response assembly -------------------------------------------------

    def _load(self, conn, session_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise ExecutionError(
                f"执行会话 {session_id} 不存在或已失效",
                "unknown_session",
                404,
            )
        return row

    @staticmethod
    def _steps(row: sqlite3.Row) -> List[dict]:
        return json.loads(row["steps_json"])

    def _block(self, row: sqlite3.Row, confirmed: int) -> dict:
        steps = self._steps(row)
        nxt = steps[confirmed] if confirmed < row["total_steps"] else None
        last = steps[confirmed - 1] if confirmed > 0 else None
        return {
            "sessionId": row["session_id"],
            "routeDigest": row["route_digest"],
            "start": row["start_node"],
            "totalSteps": row["total_steps"],
            "confirmed": confirmed,
            "cursor": self.encode_cursor(
                row["session_id"], row["route_digest"], confirmed
            ),
            "complete": confirmed == row["total_steps"],
            "next": nxt,
            "last": last,
        }

    def _result_body(self, row: sqlite3.Row, confirmed: int) -> dict:
        body = dict(json.loads(row["result_json"]))
        body["execution"] = self._block(row, confirmed)
        return body

    @staticmethod
    def _receipt_reply(row: sqlite3.Row) -> Tuple[int, dict]:
        return row["http_status"], json.loads(row["response_json"])

    # -- public API --------------------------------------------------------

    def record_failure(
        self, op_id: str, content: str, body: dict
    ) -> Tuple[int, dict]:
        """Persist a receipt for a failed start (no session is created).

        Keeps op-id idempotency even when the audit input itself is invalid.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                dup = conn.execute(
                    "SELECT * FROM receipts WHERE op_id=?", (op_id,)
                ).fetchone()
                if dup is not None:
                    conn.commit()
                    if dup["content_hash"] == content:
                        return self._receipt_reply(dup)
                    raise ExecutionError(
                        "操作标识已被不同内容使用，拒绝复用",
                        "op_reused", 409)
                fail_body = dict(body)
                conn.execute(
                    "INSERT INTO receipts(op_id, session_id, kind,"
                    " content_hash, http_status, response_json)"
                    " VALUES(?, '', 'start', ?, 200, ?)",
                    (op_id, content, json.dumps(fail_body, ensure_ascii=False)),
                )
                conn.commit()
                return 200, fail_body
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def status(self, session_id: str) -> dict:
        """Recover the stored conclusion + confirmed prefix after restart."""
        conn = self._connect()
        try:
            row = self._load(conn, session_id)
            return self._result_body(row, row["confirmed"])
        finally:
            conn.close()

    def start(
        self,
        op_id: str,
        raw_payload: dict,
        start_node: str,
        steps: List[dict],
        edge_orient: List[dict],
        result_body: dict,
    ) -> Tuple[int, dict]:
        sid = secrets.token_hex(12)
        digest = route_fingerprint(start_node, steps)
        content = start_content_hash(raw_payload)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                dup = conn.execute(
                    "SELECT * FROM receipts WHERE op_id=?", (op_id,)
                ).fetchone()
                if dup is not None:
                    conn.commit()
                    if dup["content_hash"] == content:
                        return self._receipt_reply(dup)
                    raise ExecutionError(
                        "操作标识已被不同内容使用，拒绝复用",
                        "op_reused", 409)

                block = {
                    "sessionId": sid,
                    "routeDigest": digest,
                    "start": start_node,
                    "totalSteps": len(steps),
                    "confirmed": 0,
                    "cursor": self.encode_cursor(sid, digest, 0),
                    "complete": len(steps) == 0,
                    "next": steps[0] if steps else None,
                    "last": None,
                }
                body = dict(result_body)
                body["execution"] = block
                conn.execute(
                    "INSERT INTO sessions(session_id, route_digest, start_node,"
                    " total_steps, confirmed, steps_json, edges_json,"
                    " result_json) VALUES(?,?,?,?,0,?,?,?)",
                    (sid, digest, start_node, len(steps),
                     json.dumps(steps, ensure_ascii=False),
                     json.dumps(edge_orient, ensure_ascii=False),
                     json.dumps(body, ensure_ascii=False)),
                )
                conn.execute(
                    "INSERT INTO receipts(op_id, session_id, kind,"
                    " content_hash, http_status, response_json)"
                    " VALUES(?,?, 'start', ?, 200, ?)",
                    (op_id, sid, content,
                     json.dumps(body, ensure_ascii=False)),
                )
                conn.commit()
                return 200, body
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    def advance(
        self,
        session_id: str,
        cursor: str,
        op_id: str,
        edge_id: str,
        direction: str,
        copy_no: int,
    ) -> Tuple[int, dict]:
        content = advance_content_hash(
            session_id, cursor, edge_id, direction, copy_no
        )
        conn = self._connect()
        try:
            # Reject unknown sessions before taking the write lock.
            row = self._load(conn, session_id)
            conn.execute("BEGIN IMMEDIATE")
            try:
                dup = conn.execute(
                    "SELECT * FROM receipts WHERE op_id=?", (op_id,)
                ).fetchone()
                if dup is not None:
                    conn.commit()
                    if dup["content_hash"] == content:
                        return self._receipt_reply(dup)
                    raise ExecutionError(
                        "操作标识已被不同内容使用，拒绝复用",
                        "op_reused", 409)

                # Re-read inside the transaction for serialisable behaviour
                # against concurrent tabs / workers.
                row = conn.execute(
                    "SELECT * FROM sessions WHERE session_id=?", (session_id,)
                ).fetchone()
                confirmed = row["confirmed"]
                block = self._block(row, confirmed)

                def reject(code: str, message: str, status: int = 409,
                           expected: Any = False) -> Tuple[int, dict]:
                    body: Dict[str, Any] = {
                        "ok": False, "error": message, "code": code,
                        "execution": block,
                    }
                    if expected is not False:
                        body["expected"] = expected
                    conn.execute(
                        "INSERT INTO receipts(op_id, session_id, kind,"
                        " content_hash, http_status, response_json)"
                        " VALUES(?,?, 'advance', ?, ?, ?)",
                        (op_id, session_id, content, status,
                         json.dumps(body, ensure_ascii=False)),
                    )
                    conn.commit()
                    return status, body

                # 1) cursor must be a valid signature for this session/digest
                #    and must equal the current prefix.
                try:
                    cur = self.decode_cursor(cursor)
                except ValueError:
                    return reject(
                        "stale_cursor",
                        "游标无效或已被篡改，请按当前会话状态重试")
                if (cur["s"] != session_id or cur["d"] != row["route_digest"]
                        or cur["p"] != confirmed):
                    return reject(
                        "stale_cursor",
                        "游标已过期（会话已推进或属于其他路线），"
                        "请使用最新游标重试")

                steps = self._steps(row)
                if confirmed >= row["total_steps"]:
                    return reject(
                        "already_complete",
                        "全部副本已核对完成并回到检修口，无需继续推进",
                        expected=None)

                expected = steps[confirmed]
                orient = {
                    e["id"]: (e["u"], e["v"])
                    for e in json.loads(row["edges_json"])
                }

                # 2) only the exact next expected step may extend the prefix.
                if edge_id not in orient:
                    return reject(
                        "mismatch",
                        f"管段 {edge_id} 不属于本次路线绑定的管网",
                        expected=expected)
                if edge_id != expected["edgeId"]:
                    return reject(
                        "mismatch",
                        f"下一步应为管段 {expected['edgeId']}，"
                        f"收到 {edge_id}",
                        expected=expected)
                u, v = orient[edge_id]
                exp_dir = "forward" if expected["from"] == u else "reverse"
                if direction != exp_dir:
                    human = f"{u} → {v}" if exp_dir == "forward" else f"{v} → {u}"
                    return reject(
                        "mismatch",
                        f"管段 {edge_id} 的下一步方向应为 {human}",
                        expected=expected)
                if copy_no != expected["copy"]:
                    return reject(
                        "mismatch",
                        f"管段 {edge_id} 的下一副本号应为 {expected['copy']}，"
                        f"收到 {copy_no}",
                        expected=expected)

                # 3) accept: single prefix extension, receipt = first result.
                new_confirmed = confirmed + 1
                conn.execute(
                    "UPDATE sessions SET confirmed=? WHERE session_id=?",
                    (new_confirmed, session_id))
                new_row = conn.execute(
                    "SELECT * FROM sessions WHERE session_id=?", (session_id,)
                ).fetchone()
                new_block = self._block(new_row, new_confirmed)
                body = {"ok": True, "execution": new_block}
                conn.execute(
                    "INSERT INTO receipts(op_id, session_id, kind,"
                    " content_hash, http_status, response_json)"
                    " VALUES(?,?, 'advance', ?, 200, ?)",
                    (op_id, session_id, content,
                     json.dumps(body, ensure_ascii=False)))
                conn.commit()
                return 200, body
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Process-wide default store (lazily built so tests can inject their own)
# ---------------------------------------------------------------------------

_default_store: Optional[ExecutionStore] = None
_default_lock = threading.Lock()


def get_store() -> ExecutionStore:
    global _default_store
    store = _default_store
    if store is None:
        with _default_lock:
            if _default_store is None:
                _default_store = ExecutionStore()
            store = _default_store
    return store


def set_default_store(store: Optional[ExecutionStore]) -> None:
    global _default_store
    with _default_lock:
        _default_store = store
