"""Behavior observations get their own call, and the call stays in its lane.

Doc 19.5, measured on eight real meetings 2026-08-15: the type INLINE as
one of fifteen produced 4 observations with Haiku and ZERO with Sonnet.
A dedicated call with the same cheap model produced 48. Prod runs Haiku,
so replaying 41 perishable meetings under the inline config would have
returned roughly twenty observations across the lot.

These tests are mostly about what the call must REFUSE to do. A second
writer at ingest is a second source of truth unless it is fenced.
"""

import pathlib

from contextquilt.services.behavior_extraction import (
    BEHAVIOR_SYSTEM,
    MAX_OBSERVATIONS,
    MIN_TRANSCRIPT_CHARS,
    build_behavior_content,
    parse_behavior_response,
    worth_a_call,
)

WORKER = pathlib.Path("src/worker.py").read_text()


def _resp(*pairs):
    return {"observations": [{"text": t, "owner": o} for t, o in pairs]}


# --- what it produces --------------------------------------------------

def test_an_observation_becomes_a_patch_the_normal_sink_understands():
    got = parse_behavior_response(
        _resp(("Asked for the cost breakdown before agreeing", "Denby")))
    assert got == [{"type": "behavior",
                    "value": {"text": "Asked for the cost breakdown before agreeing",
                              "owner": "Denby"}}]


def test_the_user_never_becomes_an_observation():
    """This corpus is about the people the user works with. The main
    extraction's sanitizers refuse a self person patch too; this stops
    the call spending a slot rather than relying on that catch."""
    got = parse_behavior_response(
        _resp(("Ran the meeting from the agenda", "Scott"),
              ("Pushed back on the date", "Denby")),
        user_label="Scott",
    )
    assert [p["value"]["owner"] for p in got] == ["Denby"]


def test_the_you_marker_forms_are_refused_too():
    got = parse_behavior_response(
        _resp(("Did a thing", "(you)"), ("Did another", "you")))
    assert got == []


def test_duplicate_observations_collapse():
    got = parse_behavior_response(
        _resp(("Asked for numbers", "Denby"), ("asked for NUMBERS", "denby")))
    assert len(got) == 1


def test_the_per_call_ceiling_holds():
    got = parse_behavior_response(
        _resp(*[(f"did thing {i}", f"Person{i}") for i in range(40)]))
    assert len(got) == MAX_OBSERVATIONS


# --- what it refuses ---------------------------------------------------

def test_garbage_costs_a_call_and_no_write():
    for junk in ("not json", None, 42, {"observations": "nope"}, "{bad"):
        assert parse_behavior_response(junk) == []


def test_a_row_missing_either_half_is_dropped():
    """An observation with no owner cannot reach a person, and the
    ownership edge is the only route anything has to one."""
    assert parse_behavior_response({"observations": [
        {"text": "someone did something"},
        {"owner": "Denby"},
        {"text": "", "owner": "Denby"},
    ]}) == []


def test_a_short_transcript_is_not_worth_a_call():
    assert worth_a_call("x" * (MIN_TRANSCRIPT_CHARS - 1)) is False
    assert worth_a_call("x" * MIN_TRANSCRIPT_CHARS) is True
    assert worth_a_call(None) is False


# --- the prompt keeps the call in its lane -----------------------------

def test_the_prompt_forbids_the_verdict_shape():
    """'Is defensive about code review feedback' is the shape guardrail
    12b exists to stop, and it is cheaper to refuse in the prompt than
    to sanitize afterwards."""
    assert "verdict, not an observation" in BEHAVIOR_SYSTEM


def test_the_prompt_hands_commitments_back_to_the_stage_that_owns_them():
    assert "Another stage owns it." in BEHAVIOR_SYSTEM
    assert "You are not summarizing the meeting" in BEHAVIOR_SYSTEM


def test_the_prompt_bans_dashes_where_the_text_is_made():
    """Claims are quoted into surfaces where dashes are banned and the
    next model copies the punctuation it reads."""
    assert "NEVER use a dash of any kind as punctuation" in BEHAVIOR_SYSTEM


def test_manifest_guidance_reaches_the_call():
    """A per-app vocabulary reaches this call the same way it reaches
    the main extraction, rather than being reinvented here."""
    content = build_behavior_content("a transcript", "app says: note hedging")
    assert "app says: note hedging" in content


# --- the wiring --------------------------------------------------------

def test_it_writes_through_the_shared_sink_not_its_own_path():
    """Ownership edges, origin stamping, ACLs, dedup and the manifest's
    storage keys all live in store_connected_patches. A second writer
    with its own path would be a second source of truth."""
    body = WORKER.split("async def _extract_behavior_observations")[1].split(
        "async def ")[0]
    assert "store_connected_patches(" in body
    assert "no_collapse_types=no_collapse_patch_types(manifest)" in body


def test_it_is_inert_unless_the_manifest_declares_the_type():
    body = WORKER.split("async def _extract_behavior_observations")[1].split(
        "async def ")[0]
    assert 'if "behavior" not in declared:' in body


def test_a_failure_here_cannot_lose_the_meeting():
    """It runs after the real extraction is already stored, so the user
    keeps everything the main pass produced."""
    body = WORKER.split("async def _extract_behavior_observations")[1].split(
        "async def ")[0]
    assert "behavior_observations_failed" in body
    assert "return 0" in body
