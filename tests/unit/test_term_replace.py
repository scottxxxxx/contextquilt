"""Fix a misheard word in one meeting (services/term_replace, 2026-10-31).

The pure half is EXECUTED here; the route's wiring is pinned by
source-reading tests at the bottom (fastapi is absent from the unit
venv), and its runtime half is the post-deploy smoke.

The match examples are ShoulderSurf's own, sent with their rule, because
the client shows our count beside theirs and a disagreement on these
would make that comparison noise.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from src.contextquilt.services import term_replace as tr

ROOT = pathlib.Path(__file__).resolve().parents[2]
MAIN = (ROOT / "src" / "main.py").read_text()


def _sub(text, term="Cora", to="Kore.ai", match_case=False):
    return tr.replace_text(text, tr.compile_term(term, match_case), to)


# --- the client's whole-word rule ---------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Cora said yes", "Kore.ai said yes"),
    ("we met Cora's team", "we met Kore.ai's team"),
    ("Cora, then lunch", "Kore.ai, then lunch"),
    ("ask cora about it", "ask Kore.ai about it"),
])
def test_matches_what_the_client_matches(text, expected):
    assert _sub(text) == (expected, 1)


@pytest.mark.parametrize("text", ["Coral reef", "decoration", "Cora2", "_Cora"])
def test_never_matches_inside_a_word(text):
    """Coral and decoration are the client's own counterexamples. A digit
    is a letter-or-number neighbour, so Cora2 is one token too."""
    new, n = _sub(text)
    if text == "_Cora":
        # The underscore is NOT a letter or number in the client's
        # \\p{L}\\p{N} boundary, so it is a boundary there, and here.
        assert n == 1
    else:
        assert (new, n) == (text, 0)


def test_a_multi_word_term_matches_across_a_line_break():
    assert _sub("talk to core\nAI tomorrow", term="core AI", to="Kore.ai") == (
        "talk to Kore.ai tomorrow", 1)


def test_match_case_is_honoured():
    assert _sub("ask cora", match_case=True) == ("ask cora", 0)
    assert _sub("ask Cora", match_case=True) == ("ask Kore.ai", 1)


def test_the_replacement_is_literal():
    """No template expansion: a backslash group in `to` is text."""
    assert _sub("Cora", to=r"\1 \g<0>") == (r"\1 \g<0>", 1)


def test_the_count_is_the_number_of_matches():
    assert _sub("Cora and cora and CORA") == ("Kore.ai and Kore.ai and Kore.ai", 3)


def test_regex_characters_in_the_term_are_escaped():
    assert _sub("we use Kore.ai daily", term="Kore.ai", to="X") == ("we use X daily", 1)
    assert _sub("we use KoreXai daily", term="Kore.ai", to="X") == ("we use KoreXai daily", 0)


# --- the value rewrite ---------------------------------------------------

def test_every_word_bearing_field_is_rewritten_and_nothing_else():
    value = {
        "text": "Send Cora the deck",
        "owner": "Cora",
        "owed_to": "Cora team",
        "deadline": "before Cora's offsite",
        "headline": "Cora deck",
        "deadline_date": "2026-11-03",
        "restatements": [{"text": "Cora deck again", "origin_id": "m2"},
                         {"text": "unrelated", "origin_id": "m3"}],
    }
    new, n, keys = tr.replace_in_value(value, tr.compile_term("Cora"), "Kore.ai")
    # text, owner, owed_to, the spoken deadline, and one restatement
    assert n == 5
    assert set(keys) == {"text", "owner", "owed_to", "deadline", "restatements"}
    assert new["text"] == "Send Kore.ai the deck"
    assert new["restatements"][0] == {"text": "Kore.ai deck again", "origin_id": "m2"}
    assert new["restatements"][1] == {"text": "unrelated", "origin_id": "m3"}
    # headline is derived and recomputed by the route, never regexed here
    assert new["headline"] == "Cora deck"
    assert new["deadline_date"] == "2026-11-03"


def test_the_input_value_is_never_mutated():
    value = {"text": "Cora", "restatements": [{"text": "Cora"}]}
    tr.replace_in_value(value, tr.compile_term("Cora"), "Kore.ai")
    assert value == {"text": "Cora", "restatements": [{"text": "Cora"}]}


def test_a_second_application_changes_nothing():
    """Idempotent, which is what lets a retry after a lost 2xx be safe."""
    p = tr.compile_term("Cora")
    once, n1, _ = tr.replace_in_value({"text": "Cora said"}, p, "Kore.ai")
    twice, n2, keys = tr.replace_in_value(once, p, "Kore.ai")
    assert (n1, n2, keys) == (1, 0, [])
    assert twice == once


def test_undo_is_the_swap():
    p = tr.compile_term("Cora")
    fixed, _, _ = tr.replace_in_value({"text": "Cora said"}, p, "Kore.ai")
    back, n, _ = tr.replace_in_value(fixed, tr.compile_term("Kore.ai"), "Cora")
    assert back == {"text": "Cora said"} and n == 1


def test_cues_are_rewritten_lowercase():
    assert tr.replace_cue("cora pricing", tr.compile_term("Cora"), "Kore.ai") == "kore.ai pricing"
    assert tr.replace_cue("coral reef", tr.compile_term("Cora"), "Kore.ai") is None


# --- entities: user-wide, so renamed only when provably this meeting's ---

def test_a_person_seen_only_in_this_meeting_is_renamed():
    assert tr.entity_verdict("person", ["m1", "m1"], "m1", False) == tr.RENAMED


def test_a_person_seen_elsewhere_is_left_alone():
    assert tr.entity_verdict("person", ["m1", "m2"], "m1", False) == tr.SKIP_OTHER_MEETINGS


def test_no_meeting_record_is_never_guessed_at():
    assert tr.entity_verdict("person", [], "m1", False) == tr.SKIP_NO_MEETING_RECORD
    assert tr.entity_verdict("company", ["m1"], "m1", False) == tr.SKIP_NO_MEETING_RECORD


def test_a_taken_target_name_is_a_merge_not_a_rename():
    assert tr.entity_verdict("person", ["m1"], "m1", True) == tr.SKIP_TARGET_EXISTS


# --- validation ----------------------------------------------------------

@pytest.mark.parametrize("body,code", [
    ({"from": "", "to": "x"}, "INVALID_TERM"),
    ({"from": "   ", "to": "x"}, "INVALID_TERM"),
    ({"to": "x"}, "INVALID_TERM"),
    ({"from": 3, "to": "x"}, "INVALID_TERM"),
    ({"from": "Cora"}, "INVALID_REPLACEMENT"),
    ({"from": "Cora", "to": ""}, "INVALID_REPLACEMENT"),
    ({"from": "Cora", "to": "x", "match_case": "yes"}, "INVALID_MATCH_CASE"),
])
def test_unusable_bodies_are_refused(body, code):
    assert tr.validate_body(body)["code"] == code


def test_a_usable_body_passes():
    assert tr.validate_body({"from": "Cora", "to": "Kore.ai"}) is None
    assert tr.validate_body({"from": "Cora", "to": "Kore.ai", "match_case": True}) is None


def test_the_echo_carries_every_agreed_key():
    body = tr.response_body("m1", "meeting", from_term="Cora", to_term="Kore.ai",
                            match_case=False, matches=3, patch_ids=["a", "b"],
                            cues_changed=1, entities_renamed=[],
                            entities_skipped=[{"reason": "x"}],
                            derived_referencing=2)
    for key in ("patches_changed", "patch_ids", "entities_renamed",
                "entities_skipped_shared", "derived_referencing", "matches",
                "limits"):
        assert key in body
    assert body["patches_changed"] == 2
    assert body["entities_skipped_shared"] == 1


# --- the route's wiring (source-reading; runtime half is the smoke) -----

def _route_src() -> str:
    tree = ast.parse(MAIN)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "replace_term_in_origin":
            return ast.get_source_segment(MAIN, node) or ""
    raise AssertionError("replace_term_in_origin not found")


def test_the_route_is_registered_at_the_agreed_path():
    assert '"/v1/origins/{user_id}/{origin_type}/{origin_id}/replace-term"' in MAIN


def test_the_scope_reads_every_status():
    """A completed commitment is served in completion history and quotes
    the misheard word there, so the scope must not filter on status."""
    assert "status" not in tr.SCOPE_SQL
    assert "cp.origin_type = $2" in tr.SCOPE_SQL and "cp.origin_id = $3" in tr.SCOPE_SQL


def test_a_fix_moves_updated_at_but_never_freshness_or_mode():
    """updated_at so the delta carries the corrected text to other
    devices; never last_observed_at (nobody said it again) and never
    origin_mode (it is still what the meeting said)."""
    assert "updated_at = $2" in tr.UPDATE_SQL
    assert "last_observed_at" not in tr.UPDATE_SQL
    assert "origin_mode" not in tr.UPDATE_SQL
    writes = [v for k, v in vars(tr).items()
              if k.endswith("_SQL") and isinstance(v, str)
              and ("UPDATE" in v or "INSERT" in v)]
    assert writes and not any("origin_mode" in w or "last_observed_at" in w
                              for w in writes)


def test_the_route_uses_the_service_for_every_decision():
    src = _route_src()
    for call in ("term_replace.validate_body(", "term_replace.compile_term(",
                 "term_replace.apply(", "term_replace.response_body("):
        assert call in src, call
    # no SQL of its own: the database half is the executed service
    code = src.split('"""', 2)[2]
    assert "conn.execute(" not in code and "conn.fetch(" not in code


def test_queue_writes_happen_after_the_transaction():
    src = _route_src()
    assert src.index("async with conn.transaction():") < src.index("headline_patch")
    # the enqueue block sits outside the `async with db_pool.acquire()`
    tx_end = src.index("# After the commit, never inside it")
    assert src.index('"interaction_type": "headline_patch"') > tx_end


# --- the worker skip's placement ------------------------------------------

WORKER = (ROOT / "src" / "worker.py").read_text()


def test_the_worker_skip_runs_before_any_extraction_lane():
    """Placed after the system tasks and before every lane that extracts,
    so a replaced transcript can reach none of them."""
    body = WORKER[WORKER.index("async def process_task(self, payload"):]
    skip = body.index("if skips_extraction(payload):")
    for lane in ('if task_type == "meeting_transcript":',
                 'if task_type == "structured_patches":',
                 "await self._buffer_event(payload"):
        assert skip < body.index(lane), lane
    assert body.index('if task_type == "hydrate":') < skip
