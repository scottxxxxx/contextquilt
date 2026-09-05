"""The recall project scope rule, as text, plus the wiring in main.py.

Executing coverage lives in test_recall_project_scope_db.py (a real
Postgres). These are the cheap half: the predicate says what the module
promises, every leg uses it, and the clause that caused the leak is gone
from the flat leg.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from contextquilt.services.cue_matching import build_cue_fetch
from contextquilt.services.recall_scope import (
    FLAT_LIMIT,
    build_flat_fetch,
    build_scoped_count,
    foreign_clause,
    in_project_clause,
    origins_cte,
    project_scope_clause,
)

MAIN = (Path(__file__).resolve().parents[2] / "src" / "main.py").read_text()
AGE = "AND ($4::int IS NULL OR cp.patch_type = ANY($3::text[]))"
UNIVERSAL = ["trait", "preference", "goal", "constraint"]


def _flat_leg_source():
    i = MAIN.index("# Step 4a: Flat patch query")
    return MAIN[i:MAIN.index("# Step 4b:", i)]


def test_the_cte_resolves_meetings_from_stamped_siblings_only():
    cte = origins_cte("project_id", "$1", "$2")
    assert cte.startswith("WITH origins AS MATERIALIZED (")
    assert "cp.origin_id IS NOT NULL AND cp.project_id IS NOT NULL" in cte
    assert "held AS (SELECT origin_type, origin_id FROM origins WHERE scope = $2)" in cte
    # Foreign is "some other project AND not this one", never just "other".
    assert "WHERE scope <> $2 EXCEPT SELECT origin_type, origin_id FROM held" in cte


def test_origin_type_is_coalesced_so_the_row_test_cannot_be_null():
    """A NULL inside NOT drops the row silently; every term must be boolean."""
    assert "COALESCE(cp.origin_type, '') AS origin_type" in origins_cte("project_id", "$1", "$2")
    assert "(COALESCE(cp.origin_type, ''), cp.origin_id) IN" in in_project_clause("project_id", "$2")
    assert "(COALESCE(cp.origin_type, ''), cp.origin_id) IN" in foreign_clause("project_id")


def test_held_is_stamped_or_meeting_held_and_never_null():
    held = in_project_clause("project_id", "$2")
    assert "COALESCE(cp.project_id = $2, false)" in held
    assert "cp.project_id IS NULL AND cp.origin_id IS NOT NULL" in held
    assert "IN (SELECT origin_type, origin_id FROM held)" in held


def test_no_correlated_subquery_anywhere():
    """The first cut used EXISTS over context_patches inside the filter and
    prod went from 11 ms to 570 ms (estimates, JIT, a nested loop over
    8.8 million join rows). Set membership only."""
    sql, _ = build_flat_fetch("user:u", UNIVERSAL, None, AGE, recall_project_id="P")
    assert "EXISTS" not in sql
    assert sql.count("MATERIALIZED") == 1


def test_the_whole_rule_admits_exactly_three_kinds():
    rule = project_scope_clause("project_id", "$2", "$3")
    assert rule.count("OR cp.patch_type = ANY($3::text[])") == 1
    assert "cp.project_id IS NULL AND NOT (" in rule
    assert "OR cp.project_id IS NULL OR" not in rule


def test_flat_fetch_is_two_windows_in_one_statement():
    sql, args = build_flat_fetch("user:u", UNIVERSAL, None, AGE, recall_project_id="P")
    assert sql.count("UNION ALL") == 1
    assert sql.count(f"LIMIT {FLAT_LIMIT}") == 2
    assert args == ["user:u", "P", UNIVERSAL, None]
    first, second = sql.split("UNION ALL")
    assert "NOT (COALESCE(cp.project_id = $2, false)" in second
    assert "NOT (COALESCE(cp.project_id = $2, false)" not in first
    assert "cp.patch_type = ANY($3::text[])" in second
    assert first.startswith("WITH origins AS MATERIALIZED")


def test_flat_fetch_selects_both_project_columns_for_the_scoped_hit_check():
    sql, _ = build_flat_fetch("user:u", UNIVERSAL, None, AGE, recall_project="ABM")
    assert "cp.project_id, cp.project " in sql
    assert "cp.project = $2" in sql


def test_flat_fetch_refuses_an_unscoped_request():
    with pytest.raises(ValueError):
        build_flat_fetch("user:u", UNIVERSAL, None, AGE)


def test_main_flat_leg_uses_the_builder_and_lost_the_leaking_clause():
    leg = _flat_leg_source()
    assert "build_flat_fetch(" in leg
    assert "OR cp.project_id IS NULL OR" not in leg
    assert "OR cp.project IS NULL OR" not in leg
    # The unscoped branch is untouched: universal types only.
    assert "cp.patch_type = ANY($2::text[])" in leg


def test_coverage_denominator_counts_what_window_one_draws_from():
    sql, args = build_scoped_count("user:u", UNIVERSAL, 30, AGE, recall_project_id="P")
    assert "SELECT count(*)" in sql and in_project_clause("project_id", "$2") in sql
    assert AGE in sql and args == ["user:u", "P", UNIVERSAL, 30]
    assert "build_scoped_count(" in MAIN
    assert "WHERE ps.subject_key = $1 AND cp.project_id = $2\n                      AND COALESCE" not in MAIN


def test_cue_leg_uses_the_same_rule_and_carries_the_cte():
    sql, args = build_cue_fetch("user:u", ["api"], UNIVERSAL, None, AGE, recall_project_id="P")
    assert sql.startswith(origins_cte("project_id", "$1", "$5"))
    assert project_scope_clause("project_id", "$5", "$3") in sql
    assert args[4] == "P"
    unscoped, _ = build_cue_fetch("user:u", ["api"], UNIVERSAL, None, AGE)
    assert "WITH origins" not in unscoped and "project_id" not in unscoped
