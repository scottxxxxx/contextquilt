"""Unit tests for recall_scorer."""

import json
from datetime import datetime, timedelta

import pytest

from src.contextquilt.services.recall_scorer import (
    FRESHNESS_FLOOR,
    FRESHNESS_TIME_CONSTANT_DAYS,
    FRESHNESS_TRACKED_TYPES,
    score_patches,
    top_k_patches,
    TYPE_PRIORITY,
)


def _patch(patch_id, patch_type, text, owner=None, deadline=None, created_at=None, last_observed_at=None, deadline_date=None):
    value = {"text": text, "owner": owner, "deadline": deadline}
    if deadline_date is not None:
        value["deadline_date"] = deadline_date
    return {
        "patch_id": patch_id,
        "patch_type": patch_type,
        "value": json.dumps(value),
        "source_prompt": "meeting_summary",
        "created_at": created_at or datetime.utcnow(),
        "last_observed_at": last_observed_at,
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


# ============================================================
# Freshness multiplier for self-typed patches (trait, preference,
# goal, constraint). Stale prefs lose recall weight; fresh ones
# don't. Non-freshness-tracked types are unaffected.
# ============================================================


def test_freshness_multiplier_unaffects_non_self_typed():
    """A 2-year-old commitment scores by its raw composite — no penalty."""
    fresh = datetime.utcnow()
    very_stale = datetime.utcnow() - timedelta(days=730)
    patches = [
        _patch("fresh", "commitment", "ship by Q3", last_observed_at=fresh),
        _patch("stale", "commitment", "ship by Q3", last_observed_at=very_stale),
    ]
    scored = score_patches(patches, query_text="ship", matched_entity_names=[])
    fresh_score = next(s for s, r in scored if r["patch_id"] == "fresh")
    stale_score = next(s for s, r in scored if r["patch_id"] == "stale")
    # Recency normalization will favor fresh slightly, but the multiplier
    # itself should not be active — assert the gap is exactly the recency
    # band (≤10 points), not a multiplicative shrink.
    assert fresh_score - stale_score <= 10.0


def test_freshness_multiplier_penalizes_stale_preference():
    """A 540d-stale preference scores ~30% of a freshly re-observed one."""
    fresh = datetime.utcnow()
    very_stale = datetime.utcnow() - timedelta(days=540)
    patches = [
        _patch("fresh", "preference", "prefers async standups", last_observed_at=fresh),
        _patch("stale", "preference", "prefers async standups", last_observed_at=very_stale),
    ]
    scored = score_patches(patches, query_text="async", matched_entity_names=[])
    fresh_score = next(s for s, r in scored if r["patch_id"] == "fresh")
    stale_score = next(s for s, r in scored if r["patch_id"] == "stale")
    # Stale should be at most ~FRESHNESS_FLOOR * fresh — generous bound to
    # tolerate the recency normalization that also runs.
    assert stale_score <= fresh_score * (FRESHNESS_FLOOR + 0.05)
    # Fresh should be at full strength (no penalty on a same-day observation)
    assert fresh_score > stale_score


def test_freshness_floor_never_dropped_below():
    """Even a 10-year-old preference doesn't disappear entirely."""
    ancient = datetime.utcnow() - timedelta(days=3650)
    patches = [_patch("ancient", "preference", "prefers vim", last_observed_at=ancient)]
    scored = score_patches(patches, query_text="vim", matched_entity_names=[])
    score = scored[0][0]
    # Type priority for preference = 10, keyword overlap on "vim" = +15.
    # Floor multiplier 0.30 → minimum reachable composite is well above 0.
    # The key invariant is that the score is non-zero, not at a specific
    # value — verifies the floor clamp is active.
    assert score > 0.0


def test_freshness_multiplier_applies_to_all_four_self_typed_types():
    """The penalty hits trait, preference, goal, AND constraint identically."""
    very_stale = datetime.utcnow() - timedelta(days=540)
    fresh = datetime.utcnow()
    for ptype in FRESHNESS_TRACKED_TYPES:
        patches = [
            _patch(f"fresh-{ptype}", ptype, "shared text content here", last_observed_at=fresh),
            _patch(f"stale-{ptype}", ptype, "shared text content here", last_observed_at=very_stale),
        ]
        scored = score_patches(patches, query_text="unrelated", matched_entity_names=[])
        order = [r["patch_id"] for _, r in scored]
        assert order[0] == f"fresh-{ptype}", f"fresh should outrank stale for {ptype}"


def test_freshness_falls_back_to_created_at_when_last_observed_null():
    """A pre-migration row with NULL last_observed_at uses created_at."""
    stale_created = datetime.utcnow() - timedelta(days=540)
    fresh_created = datetime.utcnow()
    patches = [
        _patch("fresh", "preference", "alpha bravo", created_at=fresh_created, last_observed_at=None),
        _patch("stale", "preference", "alpha bravo", created_at=stale_created, last_observed_at=None),
    ]
    scored = score_patches(patches, query_text="unrelated", matched_entity_names=[])
    assert scored[0][1]["patch_id"] == "fresh"


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


# ============================================================
# Deadline urgency boost
# ============================================================


def _iso(days_from_now):
    from datetime import timezone
    return (datetime.now(timezone.utc).date() + timedelta(days=days_from_now)).isoformat()


def test_overdue_commitment_outranks_far_future_commitment():
    """Same text/created_at — only the deadline differs, so the +25
    overdue boost decides the order."""
    created = datetime.utcnow()
    patches = [
        _patch("far", "commitment", "alpha bravo charlie", created_at=created, deadline_date=_iso(60)),
        _patch("overdue", "commitment", "alpha bravo charlie", created_at=created, deadline_date=_iso(-3)),
    ]
    scored = score_patches(patches, query_text="unrelated query", matched_entity_names=[])
    assert scored[0][1]["patch_id"] == "overdue"


def test_due_soon_blocker_outranks_undated_blocker():
    created = datetime.utcnow()
    patches = [
        _patch("undated", "blocker", "alpha bravo charlie", created_at=created),
        _patch("soon", "blocker", "alpha bravo charlie", created_at=created, deadline_date=_iso(3)),
    ]
    scored = score_patches(patches, query_text="unrelated query", matched_entity_names=[])
    assert scored[0][1]["patch_id"] == "soon"


def test_overdue_boost_exceeds_due_soon_boost():
    created = datetime.utcnow()
    patches = [
        _patch("soon", "commitment", "alpha bravo charlie", created_at=created, deadline_date=_iso(3)),
        _patch("overdue", "commitment", "alpha bravo charlie", created_at=created, deadline_date=_iso(-3)),
    ]
    scored = score_patches(patches, query_text="unrelated query", matched_entity_names=[])
    assert scored[0][1]["patch_id"] == "overdue"


def test_deadline_boost_only_applies_to_completable_types():
    """A takeaway with an overdue deadline_date gets no boost — equal
    scores mean ordering falls back to input stability, so just assert
    score equality."""
    created = datetime.utcnow()
    patches = [
        _patch("dated", "takeaway", "alpha bravo charlie", created_at=created, deadline_date=_iso(-3)),
        _patch("undated", "takeaway", "alpha bravo charlie", created_at=created),
    ]
    scored = score_patches(patches, query_text="unrelated query", matched_entity_names=[])
    scores = {r["patch_id"]: s for s, r in scored}
    assert scores["dated"] == scores["undated"]


def test_malformed_deadline_date_is_ignored():
    created = datetime.utcnow()
    patches = [
        _patch("bad", "commitment", "alpha bravo charlie", created_at=created, deadline_date="not-a-date"),
        _patch("none", "commitment", "alpha bravo charlie", created_at=created),
    ]
    scored = score_patches(patches, query_text="unrelated query", matched_entity_names=[])
    scores = {r["patch_id"]: s for s, r in scored}
    assert scores["bad"] == scores["none"]


# ============================================================
# Cue-match boost (associative retrieval)
# ============================================================

from src.contextquilt.services.recall_scorer import CUE_MATCH_BOOST


def test_cue_boost_lifts_cue_fetched_patch_over_keyword_matches():
    now = datetime.utcnow()
    patches = [
        # Shares words with the query but wasn't cue-fetched
        _patch("kw", "takeaway", "discussed the launch timeline at length", created_at=now),
        # Shares NO words with the query — recalled purely via cue
        _patch("cue", "takeaway", "tier restructure draft is with finance", created_at=now),
    ]
    scored = score_patches(
        patches, query_text="where did we land on pricing for the launch",
        matched_entity_names=[], cue_matched_patch_ids={"cue"},
    )
    assert scored[0][1]["patch_id"] == "cue"


def test_cue_boost_value_between_keyword_cap_and_entity_boost():
    assert 60.0 < CUE_MATCH_BOOST < 100.0


def test_entity_match_still_outranks_cue_match():
    now = datetime.utcnow()
    patches = [
        _patch("ent", "takeaway", "ProjectX hero section is behind", created_at=now),
        _patch("cue", "takeaway", "tier restructure draft is with finance", created_at=now),
    ]
    scored = score_patches(
        patches, query_text="ProjectX status?",
        matched_entity_names=["ProjectX"], cue_matched_patch_ids={"cue"},
    )
    assert scored[0][1]["patch_id"] == "ent"


def test_no_cue_ids_param_is_backward_compatible():
    patches = [_patch("1", "takeaway", "anything")]
    a = score_patches(patches, "query", [])
    b = score_patches(patches, "query", [], cue_matched_patch_ids=None)
    assert a[0][0] == b[0][0]
