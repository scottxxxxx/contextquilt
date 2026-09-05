"""The recall header: which entities a request names, and which Steven.

Read off the live route 2026-09-04: "RV" matched "interview" (bare
substring over an index full of two-letter names), and "Steven" in an
Immigration chat resolved to Steven Levy from another project because
he holds the alias and the Steven in the room holds none.
"""
from __future__ import annotations

from pathlib import Path

from contextquilt.services.entity_match import (
    BARE_NAME_CANDIDATES_SQL,
    bare_terms,
    disambiguate_bare_names,
    match_entity_names,
)

MAIN = (Path(__file__).resolve().parents[2] / "src" / "main.py").read_text()
INDEX = {"RV", "Raj", "Raj Kumar", "Hassan Waheed", "Don", "Mac", "QA", "Steven",
         "Steven Williams", "ServiceNow team"}


# ----------------------------------------------------------------------
# Word boundaries
# ----------------------------------------------------------------------

def test_a_short_name_inside_an_ordinary_word_does_not_match():
    got = match_entity_names(INDEX, "i have a follow up interview with onstak")
    assert "RV" not in got


def test_the_same_names_still_match_as_whole_words():
    text = "rv said raj kumar and hassan waheed are on qa duty"
    got = match_entity_names(INDEX, text)
    assert got == ["Hassan Waheed", "QA", "RV", "Raj", "Raj Kumar"]


def test_common_word_traps_from_the_live_index():
    """Don in don't, Mac in machine, Sam in same: all real index terms."""
    assert match_entity_names(INDEX, "we don't run it on the machine") == []
    assert match_entity_names(INDEX, "don and mac run it") == ["Don", "Mac"]


def test_output_is_sorted_and_case_insensitive():
    got = match_entity_names(INDEX, "STEVEN WILLIAMS and steven")
    assert got == ["Steven", "Steven Williams"]
    assert match_entity_names(set(), "steven") == []
    assert match_entity_names(INDEX, "") == []


def test_main_uses_the_boundary_matcher_and_lost_the_substring_loop():
    assert "matched_names = match_entity_names(known_entities or (), text_lower)" in MAIN
    assert "if name.lower() in text_lower:" not in MAIN


# ----------------------------------------------------------------------
# Which Steven
# ----------------------------------------------------------------------

LEVY = {"entity_id": "b-levy", "name": "Steven Levy", "entity_type": "person",
        "description": "AWS Bedrock approval"}
WILLIAMS = {"entity_id": "c-williams", "name": "Steven Williams", "entity_type": "person",
            "description": "immigration app"}
NGUYEN = {"entity_id": "d-nguyen", "name": "Steven Nguyen", "entity_type": "person",
          "description": None}


def _cand(row, term, present):
    return {**row, "term": term, "present": present}


def test_bare_terms_are_the_single_token_matches_lowercased():
    assert bare_terms(["Steven", "Steven Williams", "RV", " Raj "]) == ["steven", "rv", "raj"]
    assert bare_terms([]) == []


def test_the_steven_in_the_project_replaces_the_one_holding_the_alias():
    resolved = [LEVY]  # what the alias lookup returned
    cands = [_cand(LEVY, "steven", False), _cand(WILLIAMS, "steven", True),
             _cand(NGUYEN, "steven", False)]
    got = disambiguate_bare_names(resolved, ["Steven"], cands)
    assert [r["name"] for r in got] == ["Steven Williams"]
    assert got[0]["description"] == "immigration app"


def test_no_presence_anywhere_keeps_the_lookups_answer():
    """No evidence to override the alias means the old resolution stands."""
    cands = [_cand(LEVY, "steven", False), _cand(WILLIAMS, "steven", False)]
    got = disambiguate_bare_names([LEVY], ["Steven"], cands)
    assert [r["name"] for r in got] == ["Steven Levy"]


def test_two_present_namesakes_both_stay():
    cands = [_cand(LEVY, "steven", False), _cand(WILLIAMS, "steven", True),
             _cand(NGUYEN, "steven", True)]
    got = disambiguate_bare_names([LEVY], ["Steven"], cands)
    assert [r["name"] for r in got] == ["Steven Williams", "Steven Nguyen"]


def test_a_namesake_named_in_full_in_the_text_is_not_dropped():
    """The text said 'Steven Levy' outright; the bare term is a second
    mention, not a reason to remove a person the user named."""
    cands = [_cand(LEVY, "steven", False), _cand(WILLIAMS, "steven", True)]
    got = disambiguate_bare_names([LEVY], ["Steven", "Steven Levy"], cands)
    assert [r["name"] for r in got] == ["Steven Levy", "Steven Williams"]


def test_rows_that_are_not_candidates_are_untouched():
    org = {"entity_id": "a-onstack", "name": "Onstack", "entity_type": "org", "description": None}
    cands = [_cand(LEVY, "steven", False), _cand(WILLIAMS, "steven", True)]
    got = disambiguate_bare_names([org, LEVY], ["Onstack", "Steven"], cands)
    assert [r["name"] for r in got] == ["Onstack", "Steven Williams"]


def test_output_order_is_by_entity_id_for_byte_stability():
    cands = [_cand(WILLIAMS, "steven", True), _cand(NGUYEN, "steven", True)]
    got = disambiguate_bare_names([], ["Steven"], cands)
    assert [r["entity_id"] for r in got] == ["c-williams", "d-nguyen"]


def test_the_candidate_query_asks_the_project_and_skips_folded_rows():
    assert "pa.project_id = $3" in BARE_NAME_CANDIDATES_SQL
    assert "lower(split_part(e.name, ' ', 1)) = t.term" in BARE_NAME_CANDIDATES_SQL
    assert "e.merged_into IS NULL AND e.suppressed_at IS NULL" in BARE_NAME_CANDIDATES_SQL
    assert "e.entity_type = $4" in BARE_NAME_CANDIDATES_SQL


def test_main_runs_disambiguation_only_with_a_project_and_fails_open():
    i = MAIN.index("terms = bare_terms(matched_names)")
    block = MAIN[i:i + 1200]
    assert "if terms and recall_project_id:" in block
    assert "BARE_NAME_CANDIDATES_SQL, user_id, terms, recall_project_id" in block
    assert 'logger.warning("bare_name_disambiguation_failed"' in block
    # It runs BEFORE the graph walk, so the walk seeds from the right person.
    assert i < MAIN.index("entity_ids = [row[\"entity_id\"] for row in entity_rows]")


# ----------------------------------------------------------------------
# Relations: nearest edges first
# ----------------------------------------------------------------------

def test_relations_are_ordered_by_hop_depth_then_names():
    i = MAIN.index("-- Seed: relationships from/to matched entities")
    q = MAIN[i:i + 2200]
    assert "MIN(g.depth) AS depth" in q
    assert "ORDER BY depth, e1.name, e2.name, g.relationship_type" in q
    assert "ORDER BY g.from_entity_id, g.to_entity_id, g.relationship_type" not in q
