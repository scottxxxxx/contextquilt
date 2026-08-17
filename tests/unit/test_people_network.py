"""The 13b orbit graph, under the ratified contract.

The pure module is fully testable: deterministic label propagation,
deterministic unit-square layout (the day-over-day stability soft goal
falls out of determinism), the a < b edge pin, nullable cluster_id for
small communities, dominant project per cluster, and the stated caps.
Worker and endpoint guards pin the ego exclusion and the
serve-stored-bytes rule.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from contextquilt.services.people_network import (
    MIN_CLUSTER_SIZE,
    MIN_SHARED_MEETINGS,
    NODE_CAP,
    build_snapshot,
    label_propagation,
    layout_positions,
)

WORKER = (ROOT / "src" / "worker.py").read_text()
MAIN = (ROOT / "src" / "main.py").read_text()

NODES = [
    {"entity_id": "e1", "name": "Suresh Muchakurti", "meeting_count": 121},
    {"entity_id": "e2", "name": "Sukumar Gurugubelli", "meeting_count": 104},
    {"entity_id": "e3", "name": "Vijay Rayudu", "meeting_count": 93},
    {"entity_id": "e4", "name": "Pallavi", "meeting_count": 67},
    {"entity_id": "e5", "name": "Xhoi (Joy)", "meeting_count": 45},
    {"entity_id": "e6", "name": "Loner", "meeting_count": 3},
]
PAIRS = [
    {"a": "e1", "b": "e2", "weight": 94},
    {"a": "e1", "b": "e3", "weight": 80},
    {"a": "e2", "b": "e3", "weight": 74},
    {"a": "e1", "b": "e4", "weight": 60},
    {"a": "e3", "b": "e4", "weight": 49},
    {"a": "e4", "b": "e5", "weight": 12},
    {"a": "e2", "b": "e5", "weight": 9},
    {"a": "e1", "b": "e6", "weight": 1},  # below MIN_SHARED_MEETINGS, dropped
]
PROJECTS = {
    "e1": {"ABM": 56}, "e2": {"ABM": 50}, "e3": {"ABM": 40},
    "e4": {"ABM": 30}, "e5": {"OCR": 20},
}


def _snap():
    return build_snapshot(NODES, PAIRS, PROJECTS, "2026-08-11T00:00:00Z")


def test_snapshot_is_deterministic():
    assert _snap() == _snap()


def test_edges_are_pinned_a_lt_b_and_capped_by_min_shared():
    s = _snap()
    for e in s["edges"]:
        assert e["a"] < e["b"]
        assert e["weight"] >= MIN_SHARED_MEETINGS
    assert not any(e["a"] == "e1" and e["b"] == "e6" for e in s["edges"])


def test_dense_core_clusters_and_carries_dominant_project():
    s = _snap()
    by_id = {n["entity_id"]: n for n in s["nodes"]}
    core = {by_id[e]["cluster_id"] for e in ("e1", "e2", "e3", "e4")}
    assert len(core) == 1 and None not in core
    c = next(c for c in s["clusters"] if c["cluster_id"] == core.pop())
    assert c["dominant_project_id"] == "ABM"
    assert c["member_count"] >= 4


def test_small_communities_render_unclustered():
    """MIN_CLUSTER_SIZE dissolves tiny communities to a null cluster_id
    (dimmed and unlabeled per the ratified answer 4)."""
    labels = label_propagation(["a", "b"], [("a", "b", 5)])
    assert MIN_CLUSTER_SIZE > 2
    assert labels == {"a": None, "b": None}


def test_positions_unit_square_and_stable_under_unchanged_membership():
    s = _snap()
    assert {p["entity_id"] for p in s["positions"]} == {
        n["entity_id"] for n in s["nodes"]
    }
    for p in s["positions"]:
        assert 0.0 <= p["x"] <= 1.0 and 0.0 <= p["y"] <= 1.0
    ids = [n["entity_id"] for n in NODES]
    cl = label_propagation(ids, [(min(p["a"], p["b"]), max(p["a"], p["b"]), p["weight"]) for p in PAIRS])
    assert layout_positions(ids, cl) == layout_positions(ids, cl)


def test_envelope_states_version_computed_at_and_caps():
    s = _snap()
    assert s["version"] == 1
    assert s["computed_at"] == "2026-08-11T00:00:00Z"
    assert s["caps"]["nodes"] == NODE_CAP
    assert s["caps"]["min_shared_meetings"] == MIN_SHARED_MEETINGS


def test_worker_excludes_the_ego_and_caps_nodes():
    body = WORKER.split("async def _build_network_snapshot")[1].split(
        "async def _consolidate_user_people"
    )[0]
    assert "e.self_at IS NULL" in body
    assert "merged_into IS NULL AND e.suppressed_at IS NULL" in body
    assert "network_node_cap()" in WORKER


def test_endpoint_serves_stored_bytes_and_an_honest_empty():
    body = MAIN.split("async def people_network")[1].split("@app.get")[0]
    assert "people_network_snapshots" in body
    assert '"computed_at": None' in body
    assert "build_snapshot" not in body  # never computed on the read path


def test_route_name_avoids_the_taken_graph_path():
    assert '@app.get("/v1/people/{user_id}/network"' in MAIN


def test_the_synchronous_whole_quilt_render_stays_deleted():
    """`GET /v1/quilt/{user_id}/graph` was removed 2026-08-17.

    It laid out every active patch with graphviz sfdp on the request
    path. Measured on prod: 3,550 nodes and 6,180 edges took 60.3s, of
    which 60.2s was the layout and 91ms was the database. Every caller
    timed out long before that, so CQ logged 200 while the phone showed
    504, and the 6MB result was never once delivered to a user.

    Worse than slow: `dot.pipe()` is a blocking subprocess inside an
    `async def` with no thread offload, so each call froze one of four
    uvicorn event loops and pegged one of the host's two cores for a
    minute, next to a recall path budgeted in single digit ms.

    This test exists so the shape does not come back. If a graph is
    wanted again, `people_network` above is the pattern: the worker
    computes it and the read path serves stored bytes.
    """
    assert '"/v1/quilt/{user_id}/graph"' not in MAIN
    assert "import graphviz" not in MAIN
    assert "sfdp" not in MAIN
