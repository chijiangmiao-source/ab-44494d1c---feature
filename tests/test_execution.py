"""Tests for the resumable execution check-off (sessions, receipts, cursors)."""

import threading

import pytest

from app.execution import (
    ExecutionStore,
    advance_execution,
    audit_fingerprint,
    get_execution,
    start_execution,
)
from app.server import create_app, run_audit
from app.solver import audit

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
def store(tmp_path):
    return ExecutionStore(str(tmp_path / "exec.db"))


def start_ok(store, op="op-start-1", payload=K4):
    r = start_execution(store, {"opId": op, "audit": payload})
    assert r["ok"], r
    return r


def step_payload(state, op, step=None, cursor=None):
    want = step if step is not None else state["nextStep"]
    return {
        "sessionId": state["sessionId"],
        "opId": op,
        "expectedCursor": state["cursor"] if cursor is None else cursor,
        "step": {
            "edgeId": want["edgeId"],
            "from": want["from"],
            "to": want["to"],
            "copy": want["copy"],
        },
    }


def walk_all(store, state, op_prefix="walk"):
    for i in range(state["totalSteps"]):
        r = advance_execution(store, step_payload(state, f"{op_prefix}-{i}"))
        assert r["ok"], r
        state = r
    return state


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


def test_start_binds_route_summary_and_depot(store):
    r = start_ok(store)
    audit_resp = run_audit(K4)
    assert r["auditId"] == audit_resp["auditId"]
    assert r["start"] == "A"
    assert r["status"] == "active"
    assert r["cursor"] == 0
    assert r["totalSteps"] == len(audit_resp["route"]) == 8
    assert r["canonicalVector"] == "001100"
    assert r["nextStep"] == r["route"][0]
    assert r["replayed"] is False
    assert r["returnedToDepot"] is False


def test_start_idempotent_replay_returns_first_result(store):
    first = start_ok(store, op="op-s")
    second = start_execution(store, {"opId": "op-s", "audit": K4})
    assert second["ok"] and second["replayed"] is True
    assert second["sessionId"] == first["sessionId"]
    # only one session exists: advancing via the replayed id's session works once
    assert get_execution(store, first["sessionId"])["cursor"] == 0


def test_start_op_reuse_with_different_content_is_stably_rejected(store):
    start_ok(store, op="op-dup")
    for _ in range(2):  # stable: rejected every time
        r = start_execution(store, {"opId": "op-dup", "audit": TRIANGLE})
        assert r["ok"] is False and r["code"] == "op_conflict"


def test_start_with_invalid_audit_returns_audit_error(store):
    r = start_execution(store, {"opId": "op-bad", "audit": {"nodes": ["A"], "edges": []}})
    assert r["ok"] is False and r["code"] == "audit_invalid"
    assert "fields" in r and "locations" in r
    # failed start leaves no receipt: the same op id may be reused correctly
    assert start_ok(store, op="op-bad")["ok"] is True


@pytest.mark.parametrize("op_id", ["", "has space", "中文标识", "x" * 65, None, 42])
def test_op_id_must_be_printable_ascii(store, op_id):
    r = start_execution(store, {"opId": op_id, "audit": K4})
    assert r["ok"] is False and r["code"] == "bad_op_id"
    st = start_ok(store, op="op-for-step")
    r = advance_execution(store, step_payload(st, op_id if op_id is not None else ""))
    assert r["ok"] is False and r["code"] == "bad_op_id"


# ---------------------------------------------------------------------------
# step
# ---------------------------------------------------------------------------


def test_step_advances_only_exact_next_step(store):
    st = start_ok(store)
    r = advance_execution(store, step_payload(st, "op-1"))
    assert r["ok"] and r["cursor"] == 1 and r["replayed"] is False
    assert r["confirmedStep"] == st["route"][0]
    assert r["nextStep"] == st["route"][1]


def test_step_mismatch_never_partially_advances(store):
    st = start_ok(store)
    want = st["nextStep"]
    variants = [
        {"edgeId": "zz", "from": want["from"], "to": want["to"], "copy": want["copy"]},
        {"edgeId": want["edgeId"], "from": want["to"], "to": want["from"],
         "copy": want["copy"]},  # reversed direction
        {"edgeId": want["edgeId"], "from": want["from"], "to": want["to"], "copy": 2},
    ]
    for i, bad_step in enumerate(variants):
        r = advance_execution(store, step_payload(st, f"op-bad-{i}", step=bad_step))
        assert r["ok"] is False and r["code"] == "step_mismatch"
        assert r["expectedStep"] == want
    # nothing advanced; the correct step is still accepted afterwards
    assert get_execution(store, st["sessionId"])["cursor"] == 0
    r = advance_execution(store, step_payload(st, "op-good"))
    assert r["ok"] and r["cursor"] == 1


def test_step_idempotent_replay_does_not_double_advance(store):
    st = start_ok(store)
    payload = step_payload(st, "op-replay")
    first = advance_execution(store, payload)
    assert first["ok"] and first["cursor"] == 1
    replay = advance_execution(store, payload)
    assert replay["ok"] and replay["replayed"] is True
    assert replay["cursor"] == 1  # first result, not a second advance
    assert get_execution(store, st["sessionId"])["cursor"] == 1


def test_step_op_reuse_with_different_content_is_stably_rejected(store):
    st = start_ok(store)
    advance_execution(store, step_payload(st, "op-x"))
    other = dict(step_payload(st, "op-x"))
    other["step"] = dict(other["step"], copy=other["step"]["copy"] + 1)
    for _ in range(2):
        r = advance_execution(store, other)
        assert r["ok"] is False and r["code"] == "op_conflict"
    assert get_execution(store, st["sessionId"])["cursor"] == 1


def test_stale_cursor_is_rejected_without_advancing(store):
    st = start_ok(store)
    advance_execution(store, step_payload(st, "op-a"))
    # resubmit the *next* step but based on the old cursor (e.g. second tab)
    r = advance_execution(store, step_payload(st, "op-b", step=st["route"][1], cursor=0))
    assert r["ok"] is False and r["code"] == "stale_cursor"
    assert r["cursor"] == 1  # error carries the authoritative state for resync
    assert get_execution(store, st["sessionId"])["cursor"] == 1


def test_unknown_session_rejected(store):
    r = advance_execution(
        store,
        {"sessionId": "s" + "0" * 32, "opId": "op-z", "expectedCursor": 0,
         "step": {"edgeId": "e1", "from": "A", "to": "B", "copy": 1}},
    )
    assert r["ok"] is False and r["code"] == "not_found"
    assert get_execution(store, "s" + "0" * 32)["code"] == "not_found"


def test_completion_requires_every_copy_in_order(store):
    st = start_ok(store)
    final = walk_all(store, st)
    assert final["status"] == "completed"
    assert final["cursor"] == final["totalSteps"]
    assert final["nextStep"] is None
    assert final["returnedToDepot"] is True
    assert final["route"][-1]["to"] == st["start"]  # back at the depot
    # further steps are refused
    r = advance_execution(store, step_payload(final, "op-extra",
                                              step=final["route"][0]))
    assert r["ok"] is False and r["code"] == "completed"


def test_eulerian_network_execution(store):
    st = start_ok(store, op="op-eu", payload=TRIANGLE)
    assert st["totalSteps"] == 3
    final = walk_all(store, st, op_prefix="eu")
    assert final["status"] == "completed" and final["returnedToDepot"] is True


# ---------------------------------------------------------------------------
# persistence & binding
# ---------------------------------------------------------------------------


def test_cursor_and_receipts_survive_store_reopen(tmp_path):
    db = str(tmp_path / "exec.db")
    store1 = ExecutionStore(db)
    st = start_ok(store1)
    advance_execution(store1, step_payload(st, "op-p1"))
    st2 = advance_execution(store1, step_payload(
        get_execution(store1, st["sessionId"]), "op-p2"))
    assert st2["cursor"] == 2

    # simulate a web-process restart: a brand-new store over the same file
    store2 = ExecutionStore(db)
    restored = get_execution(store2, st["sessionId"])
    assert restored["ok"] and restored["cursor"] == 2
    assert restored["nextStep"] == st["route"][2]
    # receipts survived too: replaying an old op returns its first result
    replay = advance_execution(store2, step_payload(st, "op-p1"))
    assert replay["ok"] and replay["replayed"] is True and replay["cursor"] == 1
    assert get_execution(store2, st["sessionId"])["cursor"] == 2


def test_session_stays_bound_to_its_own_audit(store):
    st = start_ok(store)
    triangle_audit = run_audit(TRIANGLE)
    assert triangle_audit["auditId"] != st["auditId"]
    # re-auditing with a different route never re-binds the old session
    restored = get_execution(store, st["sessionId"])
    assert restored["auditId"] == st["auditId"] != triangle_audit["auditId"]
    assert restored["canonicalVector"] == "001100"


def test_fingerprint_changes_with_route():
    r1 = audit(K4["nodes"], K4["edges"], K4["start"])
    r2 = audit(TRIANGLE["nodes"], TRIANGLE["edges"], TRIANGLE["start"])
    r3 = audit(K4["nodes"], K4["edges"], "B")  # same graph, other depot
    assert audit_fingerprint(r1) != audit_fingerprint(r2)
    assert audit_fingerprint(r1) != audit_fingerprint(r3)
    assert audit_fingerprint(r1) == audit_fingerprint(  # deterministic
        audit(K4["nodes"], K4["edges"], K4["start"]))


# ---------------------------------------------------------------------------
# concurrency (two tabs racing the same session)
# ---------------------------------------------------------------------------


def test_concurrent_tabs_exactly_one_advance(store):
    st = start_ok(store)
    barrier = threading.Barrier(2)
    results = []

    def race(op):
        barrier.wait()
        results.append(advance_execution(store, step_payload(st, op)))

    t1 = threading.Thread(target=race, args=("op-t1",))
    t2 = threading.Thread(target=race, args=("op-t2",))
    t1.start(); t2.start(); t1.join(); t2.join()

    oks = [r for r in results if r["ok"]]
    stale = [r for r in results if not r["ok"] and r["code"] == "stale_cursor"]
    assert len(oks) == 1 and len(stale) == 1
    assert get_execution(store, st["sessionId"])["cursor"] == 1


def test_concurrent_identical_retry_returns_same_first_result(store):
    st = start_ok(store)
    payload = step_payload(st, "op-same")
    barrier = threading.Barrier(2)
    results = []

    def race():
        barrier.wait()
        results.append(advance_execution(store, payload))

    t1 = threading.Thread(target=race)
    t2 = threading.Thread(target=race)
    t1.start(); t2.start(); t1.join(); t2.join()

    assert all(r["ok"] for r in results)
    assert {r["cursor"] for r in results} == {1}
    assert sorted(r["replayed"] for r in results) == [False, True]
    assert get_execution(store, st["sessionId"])["cursor"] == 1


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    app = create_app(store=ExecutionStore(str(tmp_path / "http.db")))
    return app.test_client()


def test_http_flow_end_to_end(client):
    r = client.post("/api/audit", json=K4).get_json()
    assert r["ok"] and r["auditId"]

    st = client.post("/api/execution/start",
                     json={"opId": "h-start", "audit": K4}).get_json()
    assert st["ok"] and st["auditId"] == r["auditId"]
    sid = st["sessionId"]

    # walk the whole route over HTTP
    for i in range(st["totalSteps"]):
        want = st["nextStep"]
        resp = client.post("/api/execution/step", json={
            "sessionId": sid, "opId": f"h-step-{i}", "expectedCursor": st["cursor"],
            "step": {"edgeId": want["edgeId"], "from": want["from"],
                     "to": want["to"], "copy": want["copy"]},
        }).get_json()
        assert resp["ok"], resp
        st = resp
    assert st["status"] == "completed" and st["returnedToDepot"] is True

    # restore (what the page does after a refresh / server restart)
    got = client.get(f"/api/execution/{sid}").get_json()
    assert got["ok"] and got["cursor"] == st["totalSteps"]
    assert got["status"] == "completed"

    # idempotent replay of the first step returns its first result
    first = st["route"][0]
    replay = client.post("/api/execution/step", json={
        "sessionId": sid, "opId": "h-step-0", "expectedCursor": 0,
        "step": {"edgeId": first["edgeId"], "from": first["from"],
                 "to": first["to"], "copy": first["copy"]},
    }).get_json()
    assert replay["ok"] and replay["replayed"] is True and replay["cursor"] == 1


def test_http_error_cases(client):
    assert client.post("/api/execution/start", json={}).get_json()["code"] == "bad_op_id"
    r = client.post("/api/execution/start",
                    json={"opId": "h-bad", "audit": {"nodes": [], "edges": []}})
    body = r.get_json()
    assert body["ok"] is False and body["code"] == "audit_invalid"
    assert client.get("/api/execution/nope").get_json()["code"] == "not_found"
    r = client.post("/api/execution/step", json={
        "sessionId": "x", "opId": "h-s", "expectedCursor": 0,
        "step": {"edgeId": "e", "from": "A", "to": "B", "copy": 0},
    }).get_json()
    assert r["ok"] is False and r["code"] == "bad_request"
