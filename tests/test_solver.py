"""Tests for the route-inspection solver.

The core correctness oracle is a brute-force enumeration over all edge
subsets (practical for small graphs): a subset is feasible exactly when its
odd-degree vertex set equals the graph's odd vertices, and the optimum is the
minimum total subset weight.  Randomized cross-checks verify the DP count,
the per-edge classification, the canonical set and the Euler walk against
that oracle.
"""

import itertools
import random

import pytest

from app.solver import (
    AuditError,
    audit,
    build_nadj,
    shortest_path_masks,
    dijkstra,
)


def edge(id_, u, v, length):
    return {"id": id_, "u": u, "v": v, "length": length}


def brute_optimal(nodes, raw_edges):
    """Return (optimum_cost, list of frozensets) by exhaustive subset scan."""
    m = len(raw_edges)
    deg = {n: 0 for n in nodes}
    for e in raw_edges:
        deg[e["u"]] += 1
        deg[e["v"]] += 1
    odd = frozenset(n for n in nodes if deg[n] % 2)

    best = None
    sets = []
    for mask in range(1 << m):
        d = dict.fromkeys(nodes, 0)
        w = 0
        for i in range(m):
            if mask >> i & 1:
                e = raw_edges[i]
                d[e["u"]] += 1
                d[e["v"]] += 1
                w += e["length"]
        if frozenset(n for n in nodes if d[n] % 2) != odd:
            continue
        if best is None or w < best:
            best = w
            sets = [frozenset(i for i in range(m) if mask >> i & 1)]
        elif w == best:
            sets.append(frozenset(i for i in range(m) if mask >> i & 1))
    return best, sets, odd


def random_graph(rng, nnodes, nedges):
    nodes = [chr(65 + i) for i in range(nnodes)]
    # ensure connected: spanning path
    raw = []
    order = nodes[:]
    rng.shuffle(order)
    used_pairs = set()
    for a, b in zip(order, order[1:]):
        pair = tuple(sorted((a, b)))
        used_pairs.add(pair)
        raw.append(edge(f"e{len(raw)}", a, b, rng.randint(1, 6)))
    while len(raw) < nedges:
        a, b = rng.sample(nodes, 2)
        pair = tuple(sorted((a, b)))
        # allow parallel edges: only cap identical (id differs anyway)
        if len(used_pairs) > nnodes * (nnodes - 1) // 2:
            break
        used_pairs.add(pair)
        raw.append(edge(f"e{len(raw)}", a, b, rng.randint(1, 6)))
    return nodes, raw


def check_route(r, start):
    # every copy used exactly the right number of times, contiguous & closed
    seen = [0] * len(r.edges)
    assert r.route[0].frm == start
    cur = start
    total = 0
    for st in r.route:
        assert st.frm == cur
        assert (st.frm, st.to) in {
            (r.edges[st.edge_index].u, r.edges[st.edge_index].v),
            (r.edges[st.edge_index].v, r.edges[st.edge_index].u),
        }
        seen[st.edge_index] += 1
        total += st.length
        cur = st.to
    assert cur == start
    assert tuple(seen) == r.multiplicity
    assert total == r.total_length + r.added_length
    # duplicate numbers per edge are 1..mult
    per = {}
    for st in r.route:
        per.setdefault(st.edge_index, []).append(st.duplicate_no)
    for ei, nos in per.items():
        assert sorted(nos) == list(range(1, r.multiplicity[ei] + 1))


# ---------------------------------------------------------------------------
# Deterministic cases
# ---------------------------------------------------------------------------


def test_path_graph():
    nodes = list("ABCD")
    raw = [
        edge("e1", "A", "B", 1),
        edge("e2", "B", "C", 1),
        edge("e3", "C", "D", 1),
    ]
    r = audit(nodes, raw, "A")
    assert r.odd_vertices == ("A", "D")
    assert r.added_length == 3
    assert r.optimal_count == 1
    assert r.canonical_set == frozenset({0, 1, 2})
    assert all(v == "required" for v in r.classification.values())
    check_route(r, "A")


def test_k4_three_distinct_optimal_sets():
    # K4 with unit edges: every degree is 3; all three matchings cost 2 and
    # yield three distinct duplicate sets.
    nodes = list("ABCD")
    raw = [
        edge("e1", "A", "B", 1),  # 0
        edge("e2", "A", "C", 1),  # 1
        edge("e3", "A", "D", 1),  # 2
        edge("e4", "B", "C", 1),  # 3
        edge("e5", "B", "D", 1),  # 4
        edge("e6", "C", "D", 1),  # 5
    ]
    r = audit(nodes, raw, "A")
    assert r.odd_vertices == tuple("ABCD")
    assert r.added_length == 2
    assert r.optimal_count == 3
    # sets {e1,e6}=100001, {e2,e5}=010010, {e3,e4}=001100; 0-pref -> 001100
    assert r.bit_vector == "001100"
    assert r.canonical_set == frozenset({2, 3})
    assert all(v == "optional" for v in r.classification.values())
    check_route(r, "A")


def test_eulerian_triangle_zero_augmentation():
    nodes = list("ABC")
    raw = [edge("a", "A", "B", 3), edge("b", "B", "C", 4), edge("c", "C", "A", 5)]
    r = audit(nodes, raw, "B")
    assert r.is_eulerian
    assert r.added_length == 0
    assert r.optimal_count == 1
    assert r.bit_vector == "000"
    assert r.canonical_set == frozenset()
    assert all(v == "never" for v in r.classification.values())
    check_route(r, "B")


def test_two_equal_shortest_paths():
    # odd A,B; shortest A-B has two equal length-2 routes; a long direct
    # edge is never part of an optimum
    nodes = list("ABXY")
    raw = [
        edge("e1", "A", "X", 1),
        edge("e2", "X", "B", 1),
        edge("e3", "A", "Y", 1),
        edge("e4", "Y", "B", 1),
        edge("e5", "A", "B", 3),
    ]
    r = audit(nodes, raw, "A")
    assert r.odd_vertices == ("A", "B")
    assert r.added_length == 2
    assert r.optimal_count == 2
    assert r.canonical_set == frozenset({2, 3})  # vector 00110 < 11000
    assert r.bit_vector == "00110"
    assert r.classification[4] == "never"
    assert {r.classification[i] for i in range(4)} == {"optional"}
    check_route(r, "A")


def test_parallel_edges():
    nodes = list("AB")
    raw = [edge("p1", "A", "B", 3), edge("p2", "A", "B", 5)]
    r = audit(nodes, raw, "A")
    # two parallel edges -> degrees 2,2 -> already eulerian
    assert r.is_eulerian and r.added_length == 0

    nodes = list("ABC")
    raw = [
        edge("p1", "A", "B", 3),
        edge("p2", "A", "B", 3),
        edge("p3", "A", "B", 3),
        edge("c", "B", "C", 2),
    ]
    r = audit(nodes, raw, "C")
    # odd vertices A,C; shortest A-C = min over the 3 parallels + 2 = 5;
    # three distinct shortest paths (one per parallel edge).
    # Edges are ordered by identifier: c, p1, p2, p3.
    assert r.odd_vertices == ("A", "C")
    assert r.added_length == 5
    assert r.optimal_count == 3
    assert [e.eid for e in r.edges] == ["c", "p1", "p2", "p3"]
    assert r.classification[0] == "required"  # c
    assert all(r.classification[i] == "optional" for i in (1, 2, 3))
    # sets {c,p1}=1100, {c,p2}=1010, {c,p3}=1001 -> 0-preferred = 1001
    assert r.bit_vector == "1001"
    check_route(r, "C")


def test_required_edges_in_tree():
    # Tree A--B(4), B--C(2), B--D(2); all four vertices odd.
    # distances: AB4, CD4, AD6, AC6. Match (A,B)+(C,D) = 4+4 = 8 unique
    # optimum; path C-D is C-B-D = {b,c}. All three edges are required.
    nodes = list("ABCD")
    raw = [
        edge("a", "A", "B", 4),
        edge("b", "B", "C", 2),
        edge("c", "B", "D", 2),
    ]
    r = audit(nodes, raw, "D")
    assert set(r.odd_vertices) == {"A", "B", "C", "D"}
    assert r.added_length == 8
    assert r.optimal_count == 1
    assert r.canonical_set == frozenset({0, 1, 2})
    assert all(v == "required" for v in r.classification.values())
    check_route(r, "D")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_disconnected():
    nodes = list("ABC")
    raw = [edge("a", "A", "B", 1), edge("b", "C", "A", 1)]
    # that's connected; build a truly disconnected one
    raw = [edge("a", "A", "B", 1)]
    with pytest.raises(AuditError) as ei:
        audit(nodes, raw, "A")
    assert "不连通" in ei.value.message
    assert "edges" in ei.value.fields


def test_unknown_endpoint_duplicate_id_bad_length_bad_start():
    with pytest.raises(AuditError) as e:
        audit(list("AB"), [edge("e1", "A", "Z", 1)], "A")
    assert e.value.locations[0]["field"] == "edges"

    with pytest.raises(AuditError):
        audit(list("AB"), [edge("e1", "A", "B", 1), edge("e1", "A", "B", 2)], "A")

    with pytest.raises(AuditError):
        audit(list("AB"), [edge("e1", "A", "B", 0)], "A")
    with pytest.raises(AuditError):
        audit(list("AB"), [edge("e1", "A", "B", -3)], "A")
    with pytest.raises(AuditError):
        audit(list("AB"), [edge("e1", "A", "B", 1.5)], "A")

    with pytest.raises(AuditError):
        audit(list("AB"), [edge("e1", "A", "A", 1)], "A")  # self loop

    with pytest.raises(AuditError):
        audit(list("AB"), [edge("e1", "A", "B", 1)], "Z")


def test_node_count_limits():
    with pytest.raises(AuditError):
        audit(["A"], [edge("e1", "A", "A", 1)], "A")
    with pytest.raises(AuditError):
        audit([chr(65 + i) for i in range(19)], [], "A")


def test_ascii_rule_and_whitespace():
    with pytest.raises(AuditError):
        audit(["A", "B 2"], [edge("e1", "A", "B 2", 1)], "A")
    with pytest.raises(AuditError):
        audit(["A", "B"], [edge("e 1", "A", "B", 1)], "A")


# ---------------------------------------------------------------------------
# Randomized cross-validation against brute force
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(60))
def test_brute_force_crosscheck(seed):
    rng = random.Random(seed)
    nnodes = rng.randint(2, 6)
    nedges = rng.randint(nnodes - 1, min(12, nnodes * (nnodes - 1) // 2))
    nodes, raw = random_graph(rng, nnodes, nedges)
    if len(raw) > 16:
        raw = raw[:16]

    best, sets, odd = brute_optimal(nodes, raw)
    start = rng.choice(nodes)
    r = audit(nodes, raw, start)

    # results are ordered by edge identifier; work with id sets
    ids_sorted = [e.eid for e in r.edges]
    raw_id = {i: e["id"] for i, e in enumerate(raw)}
    id_sets = [{raw_id[i] for i in s} for s in sets]

    assert set(r.odd_vertices) == odd
    assert r.added_length == best
    assert r.optimal_count == len(sets)
    canon_ids = {r.edges[i].eid for i in r.canonical_set}
    assert canon_ids in id_sets

    # classification from oracle
    in_all = set(raw_id.values())
    in_any: set = set()
    for s in id_sets:
        in_all &= s
        in_any |= s
    for i, eid in enumerate(ids_sorted):
        want = (
            "required" if eid in in_all
            else "optional" if eid in in_any
            else "never"
        )
        assert r.classification[i] == want

    # bit vector: 0-preferred in identifier-sorted order
    def vec(idset):
        return "".join("1" if eid in idset else "0" for eid in ids_sorted)
    assert vec(canon_ids) == min(vec(s) for s in id_sets)

    check_route(r, start)


def test_largest_bounds_run_fast():
    # 18 nodes, 32 edges; worst-case odd count 18 must stay fast.
    rng = random.Random(42)
    nodes, raw = random_graph(rng, 18, 32)
    r = audit(nodes, raw, nodes[0])
    check_route(r, nodes[0])
    assert len(r.route) == 32 + len(r.canonical_set)


def test_shortest_path_sets_helper():
    nodes = list("ABXY")
    raw = [
        edge("e1", "A", "X", 1), edge("e2", "X", "B", 1),
        edge("e3", "A", "Y", 1), edge("e4", "Y", "B", 1),
    ]
    from app.solver import Edge, shortest_path_masks
    edges = [Edge(e["id"], e["u"], e["v"], e["length"], i) for i, e in enumerate(raw)]
    nadj = build_nadj(nodes, edges)
    dist = dijkstra(nodes, nadj, "A")
    ps = shortest_path_masks(nadj, "A", "B", dist)
    assert set(ps) == {0b0011, 0b1100}
