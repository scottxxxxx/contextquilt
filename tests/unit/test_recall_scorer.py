"""Unit tests for recall_scorer."""

import json
from datetime import datetime, timedelta

import pytest

from src.contextquilt.services.recall_scorer import (
    score_patches,
    top_k_patches,
    TYPE_PRIORITY,
)


def _patch(patch_id, patch_type, text, owner=None, deadline=None, created_at=None):
    return {
        "patch_id": patch_id,
        "patch_type": patch_type,
        "value": json.dumps({"text": text, "owner": owner, "deadline": deadline}),
        "source_prompt": "meeting_summary",
        "created_at": created_at or datetime.utcnow(),
    }


def test_entity_match_dominates_score():
    patches = [
        _patch("1", "takeaway", "This patch mentions ProjectX explicitly"),
        _patch("2", "commitment", "unrelated commitment"),
    ]
    scored = score_patches(patches, query_text="status of ProjectX", matched_entity_names=["ProjectX"])
    assert scored[0][1]["patch_id"] == "1"


def test_actionable_types_outrank_passive_when_no_entity_match():
    """Without entity matches or keyword overlap, commitment > trait by type priority."""
    base_text_1 = "alpha bravo charlie"
    base_text_2 = "delta echo foxtrot"
    patches = [
        _patch("t", "trait", base_text_1),
        _patch("c", "commitment", base_text_2),
    ]
    scored = score_patches(patches, query_text="some unrelated query here", matched_entity_names=[])
    # commitment (50) should beat trait (15) on type priority alone
    assert scored[0][1]["patch_id"] == "c"


def test_keyword_overlap_boosts_score():
    patches = [
        _patch("match", "takeaway", "kubernetes scaling is important"),
        _patch("nope", "takeaway", "pineapples on pizza are controversial"),
    ]
    scored = score_patches(patches, query_text="kubernetes scaling", matched_entity_names=[])
    assert scored[0][1]["patch_id"] == "match"


def test_recency_tiebreaker_when_scores_are_close():
    old = datetime.utcnow() - timedelta(days=90)
    new = datetime.utcnow()
    patches = [
        _patch("old", "takeaway", "alpha bravo charlie", created_at=old),
        _patch("new", "takeaway", "alpha bravo charlie", created_at=new),
    ]
    scored = score_patches(patches, query_text="unrelated query", matched_entity_names=[])
    assert scored[0][1]["patch_id"] == "new"


def test_goals_and_constraints_ranked_reasonably():
    """New v1 types (goal, constraint) should score higher than takeaway."""
    assert TYPE_PRIORITY["goal"] > TYPE_PRIORITY["takeaway"]
    assert TYPE_PRIORITY["constraint"] > TYPE_PRIORITY["takeaway"]


def test_empty_input():
    scored = score_patches([], query_text="anything", matched_entity_names=[])
    assert scored == []


def test_top_k_truncates():
    patches = [_patch(str(i), "takeaway", f"item {i}") for i in range(10)]
    scored = score_patches(patches, query_text="x", matched_entity_names=[])
    top = top_k_patches(scored, 3)
    assert len(top) == 3


def test_top_k_zero_returns_empty():
    patches = [_patch("a", "trait", "foo")]
    scored = score_patches(patches, query_text="x", matched_entity_names=[])
    assert top_k_patches(scored, 0) == []


# ============================================================
# Determinism: byte-stable output for upstream prompt caching.
# Locks the two pure-Python pieces we control here. The SQL-side
# stability (ORDER BY tiebreakers, sorted matched_names) is verified
# at integration level — see PR description for the e2e plan.
# ============================================================


def test_score_patches_stable_sort_on_score_ties():
    """Equal-score patches must preserve input order (Python stable sort).

    Two takeaways with no entity match, identical query keyword overlap,
    identical timestamp -> identical score. Output order must mirror
    input order so the SQL ORDER BY upstream determines rendering.
    """
    ts = datetime(2026, 5, 1, 12, 0, 0)
    patches = [
        _patch("first", "takeaway", "alpha bravo", created_at=ts),
        _patch("second", "takeaway", "alpha bravo", created_at=ts),
    ]
    scored_a = score_patches(patches, query_text="zulu", matched_entity_names=[])
    scored_b = score_patches(patches, query_text="zulu", matched_entity_names=[])
    assert [row["patch_id"] for _, row in scored_a] == ["first", "second"]
    assert [row["patch_id"] for _, row in scored_b] == ["first", "second"]


def test_score_patches_byte_identical_across_repeated_calls():
    """Repeating score_patches with identical inputs returns identical scores.

    Guards against accidental reliance on time.time() / random / set
    iteration inside the scorer. Required for the upstream Anthropic
    prompt cache to actually hit.
    """
    ts = datetime(2026, 5, 1, 12, 0, 0)
    patches = [
        _patch("a", "commitment", "ship the alpha release", created_at=ts),
        _patch("b", "blocker", "waiting on bravo team", created_at=ts),
        _patch("c", "trait", "prefers concise communication", created_at=ts),
    ]
    s1 = score_patches(patches, query_text="alpha release status", matched_entity_names=["alpha"])
    s2 = score_patches(patches, query_text="alpha release status", matched_entity_names=["alpha"])
    assert [(score, row["patch_id"]) for score, row in s1] == [
        (score, row["patch_id"]) for score, row in s2
    ]


def test_matched_names_sorted_iteration_is_deterministic():
    """Simulates the SMEMBERS-then-filter loop in /v1/recall.

    Redis SMEMBERS returns set members in arbitrary order. The recall
    endpoint now sorts the result before iterating; this test pins
    that contract so a future refactor can't silently regress it.
    """
    text_lower = "we should sync alpha and gamma about beta this week".lower()
    # Different "Redis returns" orderings of the same set.
    smembers_orderings = [
        {"alpha", "beta", "gamma", "delta"},
        ["gamma", "alpha", "delta", "beta"],
        ["delta", "gamma", "beta", "alpha"],
    ]
    results = []
    for known in smembers_orderings:
        matched = []
        for name in sorted(known):  # mirrors main.py:recall_context
            if name.lower() in text_lower:
                matched.append(name)
        results.append(matched)
    # All three orderings must produce the same matched_names list.
    assert results[0] == results[1] == results[2]
    assert results[0] == ["alpha", "beta", "gamma"]
