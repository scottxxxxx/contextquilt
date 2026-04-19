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
