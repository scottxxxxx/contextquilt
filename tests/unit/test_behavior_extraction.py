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


# --- the ops tool has to actually run ---------------------------------

def test_the_yield_tool_parses_its_own_since_argument():
    """Shipped broken in #269 and found by running it against prod:
    asyncpg binds timestamptz strictly, so a bare '2026-08-17' string
    raises DataError rather than being coerced. The unit suite never
    caught it because nothing exercised the CLI.

    I had merged an ops tool without driving it, on the same day I told
    two other teams that reading the code is not verifying the thing."""
    tool = pathlib.Path("scripts/inspect_behavior_yield.py").read_text()
    assert "datetime.fromisoformat(args.since)" in tool
    assert "tzinfo=timezone.utc" in tool
    assert "conn.fetch(QUERY, since)" in tool


def test_the_yield_tool_still_accepts_no_since():
    """The common call is no filter at all, which binds None and must
    keep working."""
    tool = pathlib.Path("scripts/inspect_behavior_yield.py").read_text()
    assert "since = None" in tool
    assert "if args.since:" in tool


# --- the lane is fenced by the same sanitizer as the main chain --------

def test_the_lane_runs_the_sanitizer_before_the_sink():
    """`sanitize_behavior_observations` was wired into the MAIN
    extraction's chain on 2026-09-01 and this lane, which writes 93% of
    the corpus, went parse -> sink untouched. One rule on two carriers
    (doc 19.11): the fix landed on the one somebody was looking at."""
    body = WORKER.split("async def _extract_behavior_observations")[1].split(
        "async def ")[0]
    assert "sanitize_behavior_observations(" in body
    assert body.index("sanitize_behavior_observations(") < body.index(
        "store_connected_patches(")


def test_the_sanitizer_accepts_the_lane_output_shape():
    """The lane feeds the parser's own output straight into the
    sanitizer. A placeholder owner is refused, a named one survives,
    and a stated preference comes back as a preference held by the
    person, exactly as it would from the main chain."""
    from contextquilt.services.extraction_schema import (
        sanitize_behavior_observations,
    )
    patches = parse_behavior_response(_resp(
        ("Asked for the cost breakdown before agreeing", "Denby"),
        ("Pushed back on the timeline", "Speaker 2"),
        ("Prefers to test and validate before committing", "Steven"),
    ))
    out = sanitize_behavior_observations({"patches": patches})
    kept = out["patches"]
    assert [p["value"]["owner"] for p in kept] == ["Denby", "Steven"]
    assert kept[0]["type"] == "behavior"
    assert kept[1]["type"] == "preference"
    assert any(e.get("label") == "held_by" and e.get("target_text") == "Steven"
               for e in kept[1]["connects_to"])
    report = out["_behavior_observations_sanitized"]
    assert report["count"] == 1
    assert report["dropped"][0]["reason"] == "placeholder_owner"


# --- gender is not observable ------------------------------------------

def test_every_prompt_that_writes_about_people_forbids_gendered_pronouns():
    """2026-09-02: a tile read "verify her Carta and ADP account access"
    about conduct that was the user's own. Gender is not in a transcript;
    a name or a voice does not state it. The rule is stated where prose
    is made, in all three prompts, in the same words."""
    from contextquilt.services import extraction_prompts, schema_prompt_builder
    rule = "NEVER use a gendered pronoun for anyone"
    assert rule in BEHAVIOR_SYSTEM
    assert rule in extraction_prompts.MEETING_SUMMARY_SYSTEM
    manifest = {"app_id": "x", "patch_types": [
        {"domain_type": "commitment", "description": "A promise."}],
        "connection_labels": [], "entity_types": []}
    assert rule in schema_prompt_builder.build_prompt(manifest)


def test_the_behavior_prompt_says_the_owner_is_who_did_it():
    assert "`owner` is the person who DID the thing, never the person it concerned" in BEHAVIOR_SYSTEM


def test_the_sanitizer_counts_gendered_pronouns_and_never_rewrites():
    from contextquilt.services.extraction_schema import (
        count_gendered_pronouns, sanitize_behavior_observations)
    assert count_gendered_pronouns("Took two minutes to verify her Carta access") == 1
    assert count_gendered_pronouns("Sheila heard the theme and shipped it") == 0
    patches = parse_behavior_response(_resp(
        ("Took two minutes to verify her Carta access", "Sarah Brooks"),
        ("Asked for the cost breakdown", "Denby"),
    ))
    out = sanitize_behavior_observations({"patches": patches})
    report = out["_behavior_observations_sanitized"]
    assert report["gendered_pronouns"] == 1
    assert report["count"] == 0
    # Counted, not rewritten: the text is byte-identical.
    assert out["patches"][0]["value"]["text"] == "Took two minutes to verify her Carta access"


def test_the_lane_logs_the_pronoun_count():
    body = WORKER.split("async def _extract_behavior_observations")[1].split(
        "async def ")[0]
    assert 'gendered_pronouns=bo.get("gendered_pronouns", 0)' in body
