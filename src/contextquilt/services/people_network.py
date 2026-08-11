"""The 13b orbit graph: person-to-person co-presence, ego excluded.

Ratified contract (2026-08-11, cross-team folder): nodes carry the
LIST-IDENTICAL name and meeting_count with a nullable cluster_id;
edges are (a, b, weight) with a < b pinned; clusters carry their
dominant project_id and member_count; positions are SERVED in the unit
square; the envelope carries computed_at and version; caps are stated,
never implied. The worker precomputes daily (no live force simulation
anywhere, per the design's build notes); the read serves the snapshot.

Everything here is pure and deterministic: same inputs, same bytes.
Layout is seeded by cluster membership and entity ids, which delivers
the ratified soft goal for free: positions are stable day over day
when membership is unchanged.
"""

from __future__ import annotations

import hashlib
import math
from typing import Dict, List, Optional, Tuple

SNAPSHOT_VERSION = 1
# Caps, stated: the contract's numbers, not tunables.
NODE_CAP = 40
MIN_SHARED_MEETINGS = 2
LABEL_PROP_ROUNDS = 10
MIN_CLUSTER_SIZE = 3  # smaller communities render unclustered (dimmed)


def _hash_unit(key: str) -> float:
    """Deterministic pseudo-random in [0, 1) from a string key."""
    h = hashlib.sha256(key.encode()).digest()
    return int.from_bytes(h[:8], "big") / 2 ** 64


def label_propagation(
    node_ids: List[str], edges: List[Tuple[str, str, int]]
) -> Dict[str, Optional[str]]:
    """Community per node by weighted label propagation, deterministic.

    Nodes iterate in sorted order every round; a node adopts the label
    with the highest total edge weight among its neighbors, ties broken
    by smallest label. Communities below MIN_CLUSTER_SIZE dissolve to
    None (the design renders them dimmed and unlabeled).
    """
    labels: Dict[str, str] = {n: n for n in node_ids}
    neighbors: Dict[str, List[Tuple[str, int]]] = {n: [] for n in node_ids}
    for a, b, w in edges:
        if a in neighbors and b in neighbors:
            neighbors[a].append((b, w))
            neighbors[b].append((a, w))

    for _ in range(LABEL_PROP_ROUNDS):
        changed = False
        for n in sorted(node_ids):
            if not neighbors[n]:
                continue
            weight_by_label: Dict[str, int] = {}
            for other, w in neighbors[n]:
                lbl = labels[other]
                weight_by_label[lbl] = weight_by_label.get(lbl, 0) + w
            best = min(
                (lbl for lbl, w in weight_by_label.items()
                 if w == max(weight_by_label.values())),
            )
            if best != labels[n]:
                labels[n] = best
                changed = True
        if not changed:
            break

    counts: Dict[str, int] = {}
    for lbl in labels.values():
        counts[lbl] = counts.get(lbl, 0) + 1
    return {
        n: (lbl if counts[lbl] >= MIN_CLUSTER_SIZE else None)
        for n, lbl in labels.items()
    }


def layout_positions(
    node_ids: List[str], cluster_of: Dict[str, Optional[str]]
) -> Dict[str, Tuple[float, float]]:
    """Unit-square positions, deterministic from ids + membership.

    Clusters sit on a ring around the center by sorted cluster key;
    members ring their cluster centroid ordered by id; unclustered
    nodes drift the outer margin by id hash. No physics anywhere.
    """
    clusters: Dict[str, List[str]] = {}
    loose: List[str] = []
    for n in node_ids:
        c = cluster_of.get(n)
        if c is None:
            loose.append(n)
        else:
            clusters.setdefault(c, []).append(n)

    pos: Dict[str, Tuple[float, float]] = {}
    ordered = sorted(clusters, key=lambda c: (-len(clusters[c]), c))
    k = max(len(ordered), 1)
    for i, ckey in enumerate(ordered):
        ang = 2 * math.pi * i / k
        cx = 0.5 + 0.28 * math.cos(ang)
        cy = 0.5 + 0.28 * math.sin(ang)
        members = sorted(clusters[ckey])
        m = len(members)
        for j, n in enumerate(members):
            r = 0.05 + 0.10 * _hash_unit(n)
            a2 = 2 * math.pi * j / m + _hash_unit(ckey) * math.pi
            pos[n] = (
                round(min(0.97, max(0.03, cx + r * math.cos(a2))), 4),
                round(min(0.97, max(0.03, cy + r * math.sin(a2))), 4),
            )
    for n in sorted(loose):
        a = 2 * math.pi * _hash_unit(n)
        r = 0.44 + 0.04 * _hash_unit(n + ":r")
        pos[n] = (
            round(min(0.97, max(0.03, 0.5 + r * math.cos(a))), 4),
            round(min(0.97, max(0.03, 0.5 + r * math.sin(a))), 4),
        )
    return pos


def build_snapshot(
    nodes: List[dict],
    pair_rows: List[dict],
    project_by_node: Dict[str, Dict[str, int]],
    computed_at_iso: str,
) -> dict:
    """The served payload. nodes: [{entity_id, name, meeting_count}]
    already ego-excluded and capped upstream; pair_rows:
    [{a, b, weight}] between surviving nodes with a < b; project_by_node
    maps entity_id -> {project_id: appearance_count} for cluster labels.
    """
    ids = [n["entity_id"] for n in nodes]
    idset = set(ids)
    edges = sorted(
        (
            (min(r["a"], r["b"]), max(r["a"], r["b"]), int(r["weight"]))
            for r in pair_rows
            if r["a"] in idset and r["b"] in idset
            and int(r["weight"]) >= MIN_SHARED_MEETINGS
        ),
    )
    cluster_of = label_propagation(ids, edges)

    # Public cluster ids: c1, c2... by size then key, never leaking the
    # seed entity id the propagation used as its label.
    seen: Dict[str, str] = {}
    members: Dict[str, List[str]] = {}
    for n, lbl in cluster_of.items():
        if lbl is not None:
            members.setdefault(lbl, []).append(n)
    for i, lbl in enumerate(
        sorted(members, key=lambda x: (-len(members[x]), x)), 1
    ):
        seen[lbl] = f"c{i}"

    clusters = []
    for lbl, mem in sorted(members.items(), key=lambda kv: seen[kv[0]]):
        proj_votes: Dict[str, int] = {}
        for n in mem:
            for pid, cnt in (project_by_node.get(n) or {}).items():
                proj_votes[pid] = proj_votes.get(pid, 0) + cnt
        dominant = (
            min(
                (p for p, c in proj_votes.items()
                 if c == max(proj_votes.values())),
            )
            if proj_votes else None
        )
        clusters.append({
            "cluster_id": seen[lbl],
            "dominant_project_id": dominant,
            "member_count": len(mem),
        })

    positions = layout_positions(ids, cluster_of)
    return {
        "version": SNAPSHOT_VERSION,
        "computed_at": computed_at_iso,
        "caps": {"nodes": NODE_CAP, "min_shared_meetings": MIN_SHARED_MEETINGS},
        "nodes": [
            {
                "entity_id": n["entity_id"],
                "name": n["name"],
                "meeting_count": n["meeting_count"],
                "cluster_id": seen.get(cluster_of.get(n["entity_id"])),
            }
            for n in nodes
        ],
        "edges": [{"a": a, "b": b, "weight": w} for a, b, w in edges],
        "clusters": clusters,
        "positions": [
            {"entity_id": n, "x": x, "y": y}
            for n, (x, y) in sorted(positions.items())
        ],
    }
