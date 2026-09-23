"""One-shot verification service.

Runs, in order:
  1. the code test suite (pytest);
  2. domain boundary checks against the solver directly
       - odd-degree network: exact co-optimal count / classification,
       - Eulerian network: zero augmentation boundary;
  3. an HTTP smoke test against the running web service
       - GET /health,
       - POST /api/audit success case,
       - POST /api/audit validation failure case;
  4. the resumable execution check-off over HTTP
       - idempotent start/step, op-id conflict, stale cursor, mismatch,
         completion only after the full ordered walk back at the depot.

BASE_URL may point at an already running instance (compose sets it to the
web service).  When unset, the script starts gunicorn locally on an
ephemeral port and tears it down afterwards.

Exits 0 only when every stage passes; the failed stage is reported.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


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

BAD = {
    "nodes": ["A", "B"],
    "edges": [{"id": "x", "u": "A", "v": "A", "length": 1}],
    "start": "A",
}


def stage(name):
    print(f"\n=== {name} ===", flush=True)


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  PASS: {msg}")


# ---------------------------------------------------------------------------
# Stage 1: pytest
# ---------------------------------------------------------------------------


def run_pytest() -> bool:
    stage("1/3 代码测试 (pytest)")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# Stage 2: solver domain boundaries
# ---------------------------------------------------------------------------


def run_domain_checks() -> bool:
    stage("2/3 奇度同优分类 & 欧拉零增程边界")
    from app.solver import audit

    # odd network: K4 unit weights -> 4 odd vertices, 3 distinct optima
    r = audit(K4["nodes"], K4["edges"], K4["start"])
    check(tuple(r.odd_vertices) == ("A", "B", "C", "D"), "K4 有四个奇度节点")
    check(r.added_length == 2, "K4 最小增程为 2")
    check(r.optimal_count == 3, "K4 同优集合数量恰为 3")
    check(r.bit_vector == "001100", "K4 规范位向量为 001100（0 优先）")
    check(
        set(r.classification.values()) == {"optional"},
        "K4 每条边均为可重复（无必重复/从不重复）",
    )
    check(
        len(r.route) == sum(r.multiplicity)
        and r.route[0].frm == "A"
        and r.route[-1].to == "A",
        "K4 规范路线闭合且副本数吻合",
    )

    # optional-vs-required mix: two equal shortest routes + a long edge
    mix_nodes = ["A", "B", "X", "Y"]
    mix_edges = [
        {"id": "p1", "u": "A", "v": "X", "length": 1},
        {"id": "p2", "u": "X", "v": "B", "length": 1},
        {"id": "p3", "u": "A", "v": "Y", "length": 1},
        {"id": "p4", "u": "Y", "v": "B", "length": 1},
        {"id": "long", "u": "A", "v": "B", "length": 3},
    ]
    r2 = audit(mix_nodes, mix_edges, "A")
    check(r2.optimal_count == 2, "双桥结构同优集合数量为 2")
    # edges are identifier-sorted: long(0), p1(1), p2(2), p3(3), p4(4)
    check(all(r2.classification[i] == "optional" for i in (1, 2, 3, 4)),
          "两条等长路径上的边为可重复")
    check(r2.classification[0] == "never", "长边从不重复")
    # candidate sets {p1,p2}=01100 and {p3,p4}=00011 -> 0-pref = 00011
    check(r2.bit_vector == "00011", "双桥规范位向量 00011")

    # Eulerian boundary: triangle
    r3 = audit(TRIANGLE["nodes"], TRIANGLE["edges"], TRIANGLE["start"])
    check(r3.is_eulerian, "三角形为欧拉管网")
    check(r3.added_length == 0, "欧拉管网零增程")
    check(r3.optimal_count == 1, "欧拉管网同优集合数量为 1（空集）")
    check(set(r3.canonical_set) == set(), "规范重复集合为空")
    check(r3.bit_vector == "000", "位向量全 0")
    check(
        all(v == "never" for v in r3.classification.values()),
        "所有边归属为从不重复",
    )
    check(len(r3.route) == 3 and r3.route[-1].to == "B",
          "欧拉回路从检修口出发并返回")
    return True


# ---------------------------------------------------------------------------
# Stage 3: HTTP smoke
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(base: str, proc, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.3)
    return False


def _post(base: str, path: str, body: dict):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode())


class _server:
    """Context manager: reuse BASE_URL or run a temporary local gunicorn."""

    def __enter__(self):
        self.base = os.environ.get("BASE_URL", "").rstrip("/")
        self.proc = None
        self._db = None
        if not self.base:
            port = _free_port()
            self.base = f"http://127.0.0.1:{port}"
            print(f"  启动临时 gunicorn: {self.base}")
            self._db = os.path.join(tempfile.mkdtemp(prefix="pipe-audit-"),
                                    "execution.db")
            env = dict(os.environ, PIPE_AUDIT_DB=self._db)
            self.proc = subprocess.Popen(
                [
                    sys.executable, "-m", "gunicorn",
                    "-w", "1", "-b", f"127.0.0.1:{port}",
                    "--timeout", "30", "app.server:app",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
        if not _wait_ready(self.base, self.proc):
            self.__exit__(None, None, None)
            raise RuntimeError("服务未在限定时间内就绪")
        print("  PASS: GET /health -> 200")
        return self.base

    def __exit__(self, *exc):
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        return False


def run_http_smoke(base: str) -> bool:
    stage("3/4 HTTP 冒烟 (health / audit 成功 / audit 失败)")
    status, body = _post(base, "/api/audit", K4)
    check(status == 200 and body.get("ok") is True,
          "POST /api/audit 奇度管网审计成功")
    check(body.get("optimalCount") == 3, "HTTP 返回同优数量为 3")
    check(body.get("addedLength") == 2, "HTTP 返回最小增程为 2")
    check(body.get("canonicalVector") == "001100",
          "HTTP 返回规范位向量 001100")
    check(len(body.get("route", [])) == 8,
          "HTTP 返回 8 步闭合路线（6 原边 + 2 重复副本）")
    check(isinstance(body.get("auditId"), str) and body["auditId"],
          "HTTP 返回审计指纹 auditId")

    status, body = _post(base, "/api/audit", TRIANGLE)
    check(body.get("ok") and body.get("addedLength") == 0,
          "HTTP 欧拉管网零增程")

    status, body = _post(base, "/api/audit", BAD)
    check(status == 200 and body.get("ok") is False,
          "非法输入返回 ok=false（HTTP 层仍为 200）")
    check("edges" in body.get("fields", []), "自环错误定位到管段表")
    check(any(l.get("row") == 0 for l in body.get("locations", [])),
          "错误位置精确到第 1 行")
    return True


def run_execution_flow(base: str) -> bool:
    stage("4/4 执行核对（幂等 / 游标 / 争用 / 完成）")
    _, audit_body = _post(base, "/api/audit", K4)
    audit_id = audit_body["auditId"]

    # start binds the route summary + depot of this audit
    _, st = _post(base, "/api/execution/start",
                  {"opId": "v-start-1", "audit": K4})
    check(st.get("ok") is True and st.get("cursor") == 0,
          "启动执行：游标从 0 开始")
    check(st.get("auditId") == audit_id and st.get("start") == "A",
          "会话绑定该次路线摘要与检修口")
    check(st.get("totalSteps") == 8 and st.get("status") == "active",
          "会话共 8 步，状态为进行中")
    sid = st["sessionId"]

    # identical retry returns the first result
    _, again = _post(base, "/api/execution/start",
                     {"opId": "v-start-1", "audit": K4})
    check(again.get("ok") and again.get("replayed") is True
          and again.get("sessionId") == sid,
          "同标识同内容重试返回首次结果（同一会话）")

    # same op id, different content -> stable rejection
    _, conflict = _post(base, "/api/execution/start",
                        {"opId": "v-start-1", "audit": TRIANGLE})
    check(conflict.get("ok") is False and conflict.get("code") == "op_conflict",
          "异参复用操作标识被稳定拒绝")

    def step(op, cursor, want):
        return _post(base, "/api/execution/step", {
            "sessionId": sid, "opId": op, "expectedCursor": cursor,
            "step": {"edgeId": want["edgeId"], "from": want["from"],
                     "to": want["to"], "copy": want["copy"]},
        })[1]

    # wrong content / stale cursor: rejected, nothing advances
    bad = dict(st["nextStep"])
    bad["to"], bad["from"] = bad["from"], bad["to"]  # reversed direction
    _, r = _post(base, "/api/execution/step", {
        "sessionId": sid, "opId": "v-bad-dir", "expectedCursor": 0,
        "step": {"edgeId": bad["edgeId"], "from": bad["from"],
                 "to": bad["to"], "copy": bad["copy"]}})
    check(r.get("ok") is False and r.get("code") == "step_mismatch",
          "方向不一致的步骤被拒绝")
    r = step("v-stale", 5, st["nextStep"])
    check(r.get("ok") is False and r.get("code") == "stale_cursor",
          "过期游标被稳定拒绝")
    _, got = _get(base, f"/api/execution/{sid}")
    check(got.get("cursor") == 0, "任何失败都不会部分推进")

    # walk the full route in order
    for i in range(st["totalSteps"]):
        r = step(f"v-walk-{i}", i, got["nextStep"])
        check(r.get("ok") and r.get("cursor") == i + 1,
              f"第 {i + 1} 步按序推进")
        got = r
    check(got.get("status") == "completed"
          and got.get("returnedToDepot") is True,
          "全部副本按序核对并回到检修口后显示完成")

    # idempotent replay of an earlier step: first result, no double advance
    first = st["route"][0]
    r = step("v-walk-0", 0, first)
    check(r.get("ok") and r.get("replayed") is True and r.get("cursor") == 1,
          "重试已成功的步骤返回首次结果，不重复推进")

    # after completion no further advance; restore still works
    r = step("v-extra", 8, first)
    check(r.get("ok") is False and r.get("code") == "completed",
          "完成后拒绝继续推进")
    _, got = _get(base, f"/api/execution/{sid}")
    check(got.get("ok") and got.get("cursor") == 8
          and got.get("status") == "completed",
          "执行游标与回执已持久化，可恢复已确认前缀")
    _, got = _get(base, "/api/execution/s00000000000000000000000000000000")
    check(got.get("ok") is False and got.get("code") == "not_found",
          "未知会话被稳定拒绝")
    return True


def main() -> int:
    failures = []
    for name, fn in (
        ("代码测试", run_pytest),
        ("同优分类/零增程边界", run_domain_checks),
    ):
        try:
            ok = fn()
        except Exception as exc:  # noqa: BLE001 - report any failure
            ok = False
            print(f"  FAIL: {name} 抛出异常: {exc!r}")
        if not ok:
            failures.append(name)

    try:
        with _server() as base:
            for name, fn in (
                ("HTTP 冒烟", run_http_smoke),
                ("执行核对流程", run_execution_flow),
            ):
                try:
                    ok = fn(base)
                except Exception as exc:  # noqa: BLE001 - report any failure
                    ok = False
                    print(f"  FAIL: {name} 抛出异常: {exc!r}")
                if not ok:
                    failures.append(name)
    except Exception as exc:  # noqa: BLE001 - report any failure
        print(f"  FAIL: 服务启动失败: {exc!r}")
        failures.extend(["HTTP 冒烟", "执行核对流程"])

    print("\n========================================")
    if failures:
        print("VERIFY 失败：" + "、".join(failures))
        return 1
    print("VERIFY 全部通过：测试 / 奇度同优分类 / 欧拉零增程 / HTTP 冒烟 / 执行核对")
    return 0


if __name__ == "__main__":
    sys.exit(main())
