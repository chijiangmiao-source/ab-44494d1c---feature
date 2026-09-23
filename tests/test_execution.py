"""Tests for resumable, idempotent on-site execution sessions."""

from __future__ import annotations

import threading

import pytest

from app.execution import ExecutionStore
from app.server import create_app


K4 = {
    "nodes": ["A", "B", "C", "D"],
    "edges": [
        {"id": "e1", "u": "A", "v": "B", "length": 1},
        {"id": "e2", "u": "A", "v": "C", "length": 1},
        {"id": "e3", "u": "A", "v": "D", "length": 1},
        {"id": "e4", "u": "B", "v": "C", "length": 1},
        {"id": "e5", "u": "B", "v": "D", "length": 1},
        {"id": "e6", "u": "C", "v": "D", "length": 1},
    ],
    "start": "A",
}

TRIANGLE = {
    "nodes": ["A", "B", "C"],
    "edges": [
        {"id": "a", "u": "A", "v": "B", "length": 3},
        {"id": "b", "u": "B", "v": "C", "length": 4},
        {"id": "c", "u": "C", "v": "A", "length": 5},
    ],
    "start": "B",
}


@pytest.fixture()
def client(tmp_path):
    store = ExecutionStore(str(tmp_path / "exec.db"))
    app = create_app(store)
    return app.test_client()


def _direction(body, step):
    edge = next(e for e in body["edges"] if e["id"] == step["edgeId"])
    return "forward" if step["from"] == edge["u"] else "reverse"


def _start(client, op_id="op-start", audit=K4):
    resp = client.post("/api/execute/start", json={"opId": op_id, "audit": audit})
    body = resp.get_json()
    assert resp.status_code == 200 and body["ok"] is True, body
    return body


def _walk(client, body, op_prefix="step", expect_complete=True):
    """Confirm the full expected prefix; return the last execution block."""
    sid = body["execution"]["sessionId"]
    cursor = body["execution"]["cursor"]
    last = body["execution"]
    for i, step in enumerate(body["route"]):
        payload = {
            "opId": f"{op_prefix}-{i}",
            "sessionId": sid,
            "cursor": cursor,
            "edgeId": step["edgeId"],
            "direction": _direction(body, step),
            "copy": step["copy"],
        }
        resp = client.post("/api/execute/advance", json=payload)
        out = resp.get_json()
        assert resp.status_code == 200 and out["ok"] is True, out
        assert out["execution"]["confirmed"] == i + 1
        last = out["execution"]
        cursor = last["cursor"]
    if expect_complete:
        assert last["complete"] is True
        assert last["confirmed"] == last["totalSteps"] == len(body["route"])
    return last


def test_start_binds_fingerprint_and_depot(client):
    body = _start(client)
    ex = body["execution"]
    assert ex["confirmed"] == 0
    assert ex["complete"] is False
    assert ex["start"] == "A"
    assert ex["totalSteps"] == len(body["route"]) == 8
    first = body["route"][0]
    assert ex["next"]["edgeId"] == first["edgeId"]
    assert ex["next"]["copy"] == first["copy"]
    assert ex["last"] is None
    # cursor is signed ASCII and differs across prefixes
    assert "." in ex["cursor"] and ex["cursor"].isascii()


def test_full_ordered_prefix_then_complete(client):
    body = _start(client)
    last = _walk(client, body)
    assert last["next"] is None
    assert last["last"]["to"] == "A"  # back at the depot
    # further advances are refused
    resp = client.post("/api/execute/advance", json={
        "opId": "after-done", "sessionId": body["execution"]["sessionId"],
        "cursor": last["cursor"], "edgeId": "e1",
        "direction": "forward", "copy": 1})
    out = resp.get_json()
    assert resp.status_code == 409 and out["code"] == "already_complete"


def test_eulerian_network_executes_without_duplicates(client):
    body = _start(client, "tri-start", TRIANGLE)
    assert body["eulerian"] is True
    last = _walk(client, body, op_prefix="tri")
    copies = [(s["edgeId"], s["copy"]) for s in body["route"]]
    assert sorted(copies) == [("a", 1), ("b", 1), ("c", 1)]
    assert last["last"]["to"] == "B"


def test_start_retry_is_idempotent_same_session(client):
    first = _start(client, "dup-op")
    second = _start(client, "dup-op")
    assert second["execution"]["sessionId"] == first["execution"]["sessionId"]
    assert second["execution"]["cursor"] == first["execution"]["cursor"]


def test_start_op_id_reuse_with_other_audit_rejected(client):
    _start(client, "reuse-me", K4)
    resp = client.post("/api/execute/start",
                       json={"opId": "reuse-me", "audit": TRIANGLE})
    out = resp.get_json()
    assert resp.status_code == 409 and out["code"] == "op_reused"


def test_advance_retry_returns_first_result(client):
    body = _start(client)
    step = body["route"][0]
    payload = {
        "opId": "adv-1",
        "sessionId": body["execution"]["sessionId"],
        "cursor": body["execution"]["cursor"],
        "edgeId": step["edgeId"],
        "direction": _direction(body, step),
        "copy": step["copy"],
    }
    first = client.post("/api/execute/advance", json=payload).get_json()
    second = client.post("/api/execute/advance", json=payload).get_json()
    assert second == first
    assert second["execution"]["confirmed"] == 1


def test_advance_op_id_reuse_with_other_params_rejected(client):
    body = _start(client)
    step = body["route"][0]
    good = {
        "opId": "adv-x",
        "sessionId": body["execution"]["sessionId"],
        "cursor": body["execution"]["cursor"],
        "edgeId": step["edgeId"],
        "direction": _direction(body, step),
        "copy": step["copy"],
    }
    assert client.post("/api/execute/advance", json=good).get_json()["ok"]
    bad = dict(good, copy=step["copy"] + 5)
    resp = client.post("/api/execute/advance", json=bad)
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "op_reused"
    # prefix untouched
    status = client.get(
        f"/api/execute/status?sessionId={body['execution']['sessionId']}"
    ).get_json()
    assert status["execution"]["confirmed"] == 1


def test_wrong_edge_direction_copy_never_advance(client):
    body = _start(client)
    sid = body["execution"]["sessionId"]
    cur = body["execution"]["cursor"]
    step = body["route"][0]
    right_dir = _direction(body, step)
    wrong_dir = "reverse" if right_dir == "forward" else "forward"
    other_id = next(
        s["edgeId"] for s in body["route"] if s["edgeId"] != step["edgeId"])

    cases = [
        ("m-edge", dict(edgeId=other_id, direction=right_dir, copy=step["copy"])),
        ("m-dir", dict(edgeId=step["edgeId"], direction=wrong_dir,
                       copy=step["copy"])),
        ("m-copy", dict(edgeId=step["edgeId"], direction=right_dir,
                        copy=step["copy"] + 1)),
    ]
    for op, fields in cases:
        resp = client.post("/api/execute/advance", json={
            "opId": op, "sessionId": sid, "cursor": cur, **fields})
        out = resp.get_json()
        assert resp.status_code == 409 and out["code"] == "mismatch", out
        assert out["expected"]["seq"] == step["seq"]
        assert out["execution"]["confirmed"] == 0

    # rejected failure receipts are themselves idempotent
    retry = client.post("/api/execute/advance", json={
        "opId": "m-edge", "sessionId": sid, "cursor": cur,
        "edgeId": other_id, "direction": right_dir, "copy": step["copy"]})
    assert retry.get_json()["execution"]["confirmed"] == 0


def test_stale_and_cross_session_cursors_rejected(client):
    body = _start(client, "s1")
    other = _start(client, "s2")
    sid = body["execution"]["sessionId"]
    cur0 = body["execution"]["cursor"]
    step = body["route"][0]
    good = {
        "opId": "go-1", "sessionId": sid, "cursor": cur0,
        "edgeId": step["edgeId"], "direction": _direction(body, step),
        "copy": step["copy"],
    }
    out = client.post("/api/execute/advance", json=good).get_json()
    cur1 = out["execution"]["cursor"]

    # old cursor no longer current
    resp = client.post("/api/execute/advance", json={
        "opId": "stale", "sessionId": sid, "cursor": cur0,
        "edgeId": body["route"][1]["edgeId"], "direction": "forward", "copy": 1})
    assert resp.get_json()["code"] == "stale_cursor"

    # cursor minted for another session/digest
    resp = client.post("/api/execute/advance", json={
        "opId": "cross", "sessionId": sid,
        "cursor": other["execution"]["cursor"],
        "edgeId": step["edgeId"], "direction": _direction(body, step),
        "copy": step["copy"]})
    assert resp.get_json()["code"] == "stale_cursor"

    # tampered token
    resp = client.post("/api/execute/advance", json={
        "opId": "forge", "sessionId": sid, "cursor": cur1[:-2] + "zz",
        "edgeId": step["edgeId"], "direction": "forward", "copy": 1})
    assert resp.get_json()["code"] == "stale_cursor"

    # unknown session
    assert client.get("/api/execute/status?sessionId=nope").status_code == 404


def test_concurrent_tabs_exactly_one_wins(tmp_path):
    db = str(tmp_path / "exec.db")
    client = create_app(ExecutionStore(db)).test_client()
    body = _start(client, "race")
    sid = body["execution"]["sessionId"]
    cur = body["execution"]["cursor"]
    step = body["route"][0]
    results = []

    def fire(op_id):
        out = client.post("/api/execute/advance", json={
            "opId": op_id, "sessionId": sid, "cursor": cur,
            "edgeId": step["edgeId"], "direction": _direction(body, step),
            "copy": step["copy"]}).get_json()
        results.append(out)

    threads = [threading.Thread(target=fire, args=(f"t{i}",))
               for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if r.get("ok")) == 1
    status = client.get(
        f"/api/execute/status?sessionId={sid}").get_json()["execution"]
    assert status["confirmed"] == 1


def test_persists_across_store_restart(tmp_path):
    db = str(tmp_path / "exec.db")
    client = create_app(ExecutionStore(db)).test_client()
    body = _start(client, "persist")
    _walk(client, body, op_prefix="p")
    sid = body["execution"]["sessionId"]

    # simulate web process restart: brand-new store/secret-reader on same db
    restarted = create_app(ExecutionStore(db)).test_client()
    status = restarted.get(
        f"/api/execute/status?sessionId={sid}").get_json()
    assert status["ok"] is True
    assert status["execution"]["confirmed"] == 8
    assert status["execution"]["complete"] is True
    # old receipt still idempotent after restart
    again = restarted.post("/api/execute/start",
                           json={"opId": "persist", "audit": K4}).get_json()
    assert again["execution"]["sessionId"] == sid


def test_new_audit_is_isolated_from_old_session(client):
    old = _start(client, "old", K4)
    new = _start(client, "new", TRIANGLE)
    assert old["execution"]["sessionId"] != new["execution"]["sessionId"]
    assert old["execution"]["routeDigest"] != new["execution"]["routeDigest"]
    # advancing the new audit never touches the old prefix
    _walk(client, new, op_prefix="new-walk")
    old_status = client.get(
        f"/api/execute/status?sessionId={old['execution']['sessionId']}"
    ).get_json()["execution"]
    assert old_status["confirmed"] == 0


def test_failed_audit_creates_no_session_but_receipts(client):
    bad = {"nodes": ["A"], "edges": [], "start": "A"}
    resp = client.post("/api/execute/start",
                       json={"opId": "bad-audit", "audit": bad})
    out = resp.get_json()
    assert out["ok"] is False and out["locations"]
    first = out
    second = client.post("/api/execute/start",
                         json={"opId": "bad-audit", "audit": bad}).get_json()
    assert second == first
    # op id cannot be reused for a valid audit afterwards
    reused = client.post("/api/execute/start",
                         json={"opId": "bad-audit", "audit": K4})
    assert reused.get_json()["code"] == "op_reused"


def test_malformed_execution_requests(client):
    assert client.post("/api/execute/start",
                       json={"opId": "white space", "audit": K4}
                       ).status_code == 400
    assert client.post("/api/execute/start", json={}).status_code == 400
    body = _start(client)
    base = {
        "opId": "v", "sessionId": body["execution"]["sessionId"],
        "cursor": body["execution"]["cursor"],
        "edgeId": "e1", "direction": "sideways", "copy": 1,
    }
    assert client.post("/api/execute/advance", json=base).status_code == 400
    base["direction"] = "forward"
    base["copy"] = 0
    assert client.post("/api/execute/advance", json=base).status_code == 400
    base["copy"] = True
    assert client.post("/api/execute/advance", json=base).status_code == 400
