"""The four observed-behaviour signals that need a reader.

Doc 21 stage 2, second cut. Migration 42 shipped the three signals that
are exact from a transcript and named the ones it was leaving out; these
are those, plus the sixth signal (the directive versus responsive turn
split) that #339's docstring dropped while accounting for six.

The property every test here is really defending is doc 19.1: THE MODEL
MAY IDENTIFY, IT MAY NOT COUNT. The model returns pointers at turns. Who
did it comes off the turn's own speaker label, and how many comes from
counting pointers that resolve. So a model that names the wrong person
cannot move a signal, and a model that cites a turn nobody took cannot
create one.
"""

import json
import pathlib

from contextquilt.services.extraction_schema import transcript_turns
from contextquilt.services import role_semantics as rs

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKER = (ROOT / "src" / "worker.py").read_text()
MIGRATION = (ROOT / "init-db" / "43_appearance_semantic_role_signals.sql").read_text()

TRANSCRIPT = """[Ana] Morning everyone, let us park pricing and do the migration first.
[Bob] Fine by me. The staging run finished clean last night.
[Ana] Bob, can you get the migration reviewed before Thursday?
[Bob] Yes, I will have it done Wednesday.
[Cara] I cannot start the rollout until legal comes back on the contract.
[Ana] Understood. Cara, please chase them today.
[Cara] I will try.
"""


def turns_of(text=TRANSCRIPT, user_label=None):
    return transcript_turns(text, user_label)


def parse(payload, text=TRANSCRIPT, user_label=None, defects=None):
    turns, self_key = turns_of(text, user_label)
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return rs.parse_role_semantics_response(body, turns, self_key, defects)


def blocks(payload, **kw):
    return parse(payload, **kw)["by_label"]


EMPTY = {"follow_ups": [], "agenda_moves": [], "upstream_deferrals": [], "turn_roles": []}


# ------------------------------------------------------------------
# The numbering IS the contract between the prompt and the counter.
# ------------------------------------------------------------------

def test_the_model_is_numbered_off_the_same_parse_that_does_the_counting():
    """An off by one between the transcript the model reads and the
    turns the counter walks would be invisible from either side: every
    signal would land one speaker away and every one of them would look
    plausible. So the rendering is generated FROM the parse, and this
    test pins that they cannot drift."""
    turns, _ = turns_of()
    rendered = rs.build_role_semantics_content(turns)
    lines = [ln for ln in rendered.splitlines() if ln and ln[0].isdigit()]
    assert len(lines) == len(turns)
    for n, (line, turn) in enumerate(zip(lines, turns), start=1):
        assert line.startswith(f"{n}. [{turn[2]}]")
    # and turn 3 in the prompt is turn 3 in the parse
    assert "can you get the migration reviewed" in lines[2]
    assert turns[2][0] == "ana"


def test_a_placeholder_turn_is_still_rendered_so_the_room_reads_in_order():
    turns, _ = turns_of("[Ana] hello\n[Speaker 2] uh\n[Ana] as I was saying\n")
    rendered = rs.build_role_semantics_content(turns)
    assert "2. [Speaker 2] uh" in rendered
    assert "3. [Ana] as I was saying" in rendered


# ------------------------------------------------------------------
# Attribution: the turn says who, never the model.
# ------------------------------------------------------------------

def test_who_did_it_is_read_off_the_turn_not_off_the_model():
    """The single most important property here. The model points at
    turn 3; turn 3 belongs to Ana in OUR parse; Ana gets the follow up.
    The model does not get a field in which to name the assigner,
    because the moment it has one it can put a signal on the wrong
    person and nothing downstream could tell."""
    by = blocks({**EMPTY, "follow_ups": [
        {"turn": 3, "assignee": "Bob", "accepted": True},
    ]})
    assert by["ana"]["follow_ups_assigned"] == 1
    assert by["ana"]["follow_ups_accepted"] == 1
    assert by["bob"]["follow_ups_assigned"] == 0
    assert by["cara"]["follow_ups_assigned"] == 0


def test_a_citation_to_a_turn_that_does_not_exist_is_dropped_not_counted():
    """The cheapest hallucination there is to catch, and catching it is
    the reason every entry is required to cite a turn at all."""
    by = blocks({**EMPTY, "follow_ups": [
        {"turn": 99, "assignee": "Bob", "accepted": True},
        {"turn": 0, "assignee": "Bob"},
        {"turn": -2, "assignee": "Bob"},
        {"turn": "not a number", "assignee": "Bob"},
        {"turn": True, "assignee": "Bob"},
        {"assignee": "Bob"},
    ]})
    assert all(b["follow_ups_assigned"] == 0 for b in by.values())


def test_a_turn_number_quoted_as_a_string_is_still_a_turn_number():
    by = blocks({**EMPTY, "agenda_moves": [{"turn": "1"}]})
    assert by["ana"]["agenda_moves"] == 1


def test_a_signal_cited_on_a_placeholder_turn_belongs_to_nobody():
    """Same discipline as 'a placeholder opener means nobody opened it'.
    Handing it to a nearby named speaker would be a guess wearing a
    count's clothes."""
    text = "[Speaker 2] right, next topic then.\n" + TRANSCRIPT
    by = blocks({**EMPTY, "agenda_moves": [{"turn": 1}]}, text=text)
    assert all(b["agenda_moves"] == 0 for b in by.values())


# ------------------------------------------------------------------
# Follow ups: what is and is not an assignment.
# ------------------------------------------------------------------

def test_taking_work_on_yourself_is_a_commitment_and_never_an_assignment():
    """The main extraction owns commitments. If this counted them the
    two would be two sources of truth about one promise, and the person
    who commits most would read as the person who assigns most."""
    by = blocks({**EMPTY, "follow_ups": [{"turn": 4, "assignee": "Bob"}]})
    assert by["bob"]["follow_ups_assigned"] == 0


def test_an_assignment_to_a_diarization_placeholder_is_not_an_assignment():
    by = blocks({**EMPTY, "follow_ups": [
        {"turn": 3, "assignee": "Speaker 2"},
        {"turn": 3, "assignee": "Unknown"},
        {"turn": 3, "assignee": "   "},
        {"turn": 3},
    ]})
    assert by["ana"]["follow_ups_assigned"] == 0


def test_only_an_explicit_yes_is_acceptance_and_the_gap_is_not_refusal():
    """Silence and "I will try" are not acceptance and they are not
    refusal either. Two counts, never a subtraction: the difference
    between them is unobserved, not negative."""
    by = blocks({**EMPTY, "follow_ups": [
        {"turn": 3, "assignee": "Bob", "accepted": True},
        {"turn": 6, "assignee": "Cara", "accepted": None},
    ]})
    assert by["ana"]["follow_ups_assigned"] == 2
    assert by["ana"]["follow_ups_accepted"] == 1


def test_one_turn_may_hand_work_to_two_people_but_not_twice_to_one():
    by = blocks({**EMPTY, "follow_ups": [
        {"turn": 3, "assignee": "Bob"},
        {"turn": 3, "assignee": "bob"},
        {"turn": 3, "assignee": "Cara"},
    ]})
    assert by["ana"]["follow_ups_assigned"] == 2


# ------------------------------------------------------------------
# Agenda moves, deferrals, and the turn split.
# ------------------------------------------------------------------

def test_one_turn_cannot_set_the_agenda_twice():
    by = blocks({**EMPTY, "agenda_moves": [{"turn": 1}, {"turn": 1}, {"turn": 6}]})
    assert by["ana"]["agenda_moves"] == 2


def test_a_deferral_lands_on_the_person_who_is_waiting():
    by = blocks({**EMPTY, "upstream_deferrals": [{"turn": 5}]})
    assert by["cara"]["upstream_deferrals"] == 1
    assert by["ana"]["upstream_deferrals"] == 0


def test_a_turn_has_one_role_and_the_first_grade_wins():
    by = blocks({**EMPTY, "turn_roles": [
        {"turn": 1, "kind": "directive"},
        {"turn": 1, "kind": "responsive"},
        {"turn": 2, "kind": "responsive"},
        {"turn": 4, "kind": "RESPONSIVE"},
        {"turn": 5, "kind": "neither"},
    ]})
    assert by["ana"]["directive_turns"] == 1 and by["ana"]["responsive_turns"] == 0
    assert by["bob"]["responsive_turns"] == 2
    assert by["cara"]["directive_turns"] == 0 and by["cara"]["responsive_turns"] == 0


def test_the_split_does_not_have_to_add_up_to_the_turn_count():
    """An unclear turn is left out on purpose. If the two were forced to
    sum to turn_count the model would be guessing at every ambiguous
    turn, and a share built on those guesses is fiction."""
    turns, _ = turns_of()
    by = blocks({**EMPTY, "turn_roles": [{"turn": 1, "kind": "directive"}]})
    graded = sum(b["directive_turns"] + b["responsive_turns"] for b in by.values())
    assert graded == 1 < len(turns)


# ------------------------------------------------------------------
# What a zero means, what nothing means.
# ------------------------------------------------------------------

def test_four_empty_lists_are_zeros_for_everyone_who_spoke():
    """The pass ran and found none. That is a different fact from the
    pass not running, and the columns keep them apart the way
    turn_count and `capacities = {}` do."""
    by = blocks(EMPTY)
    assert set(by) == {"ana", "bob", "cara"}
    assert all(v == 0 for b in by.values() for v in b.values())


def test_an_unusable_answer_writes_nothing_rather_than_confident_zeros():
    for bad in ("", "I could not find any role signals in this meeting.", "[]", "null"):
        defects: list = []
        assert parse(bad, defects=defects) == {}
    assert parse(json.dumps({"follow_ups": []}), text="") == {}
    assert parse(json.dumps(EMPTY), text="[Speaker 2] hello\n[Unknown] hi\n") == {}


def test_a_defect_is_recorded_so_a_silent_failure_is_countable():
    defects: list = []
    parse("no braces here at all", defects=defects)
    assert defects == ["no_json"]
    defects = []
    parse("{not: valid, json}", defects=defects)
    assert defects == ["bad_json"]


def test_the_user_is_separated_out_and_is_not_in_the_by_label_set():
    out = parse({**EMPTY, "agenda_moves": [{"turn": 1}]}, user_label="Ana")
    assert "ana" not in out["by_label"]
    assert out["user"]["agenda_moves"] == 1
    assert set(out["by_label"]) == {"bob", "cara"}


def test_the_marker_identifies_the_user_the_same_way_it_does_next_door():
    text = TRANSCRIPT.replace("[Ana]", "[Ana (you)]")
    out = parse(EMPTY, text=text)
    assert "ana" not in out["by_label"] and out["user"] is not None


# ------------------------------------------------------------------
# The gate, before any money is spent.
# ------------------------------------------------------------------

def test_a_short_note_is_not_worth_a_call():
    short = "[Ana] all good, nothing to report.\n"
    turns, self_key = turns_of(short)
    assert rs.worth_a_call(short, turns, self_key) is False


def test_a_room_with_only_the_user_in_it_has_nothing_to_write():
    """The user's block is computed for symmetry and stored nowhere, so
    a transcript whose only named speaker is the user would buy a call
    whose entire result is discarded."""
    text = "[Ana (you)] " + ("thinking out loud about the migration plan. " * 20) + "\n"
    turns, self_key = turns_of(text)
    assert len(text) >= rs.MIN_TRANSCRIPT_CHARS
    assert rs.worth_a_call(text, turns, self_key) is False
    # one other named voice is enough
    text2 = text + "[Bob] sounds right to me.\n"
    turns2, self_key2 = turns_of(text2)
    assert rs.worth_a_call(text2, turns2, self_key2) is True


# ------------------------------------------------------------------
# Wiring, the migration, and the promises the prompt makes.
# ------------------------------------------------------------------

def test_the_prompt_embeds_the_raw_json_shape_it_expects():
    """`AnthropicLLMClient.extract()` takes `json_schema` for interface
    parity and does NOT put it on the wire. A prompt that does not carry
    its own shape gets prose back and parses to nothing, silently."""
    for key in ("follow_ups", "agenda_moves", "upstream_deferrals", "turn_roles",
                "assignee", "accepted", "kind"):
        assert key in rs.ROLE_SEMANTICS_SYSTEM


def test_the_prompt_never_asks_the_model_for_a_number():
    """Doc 19.1. It points at turns; the counting happens in code."""
    low = rs.ROLE_SEMANTICS_SYSTEM.lower()
    assert "you never count anything" in low
    assert "how many" not in low


def test_no_dash_is_used_as_punctuation_anywhere_in_the_prompt():
    """A model copies the punctuation it is shown, and this prompt's
    output feeds nothing user facing, but the rule is the rule and it is
    cheapest to keep everywhere."""
    assert "—" not in rs.ROLE_SEMANTICS_SYSTEM
    assert "–" not in rs.ROLE_SEMANTICS_SYSTEM


def test_the_call_runs_before_the_appearance_write_not_after_it():
    """These are columns on the appearance row, and that row has exactly
    one writer with one set of re-ingest rules. A later UPDATE would be a
    second writer with rules of its own."""
    call = WORKER.index("semantic_role_signals = await self._extract_semantic_role_signals(")
    # The extraction lane's own write, not the structured lane's earlier
    # one, which passes no transcript signals at all and is correct to
    # leave every one of these columns NULL.
    write = WORKER.index("entities_stored = await store_entities(", call)
    assert call < write
    assert "speaker_semantic_signals=semantic_role_signals," in WORKER[write:write + 3000]


def test_a_failure_costs_the_signal_and_never_the_extraction():
    body = WORKER[WORKER.index("async def _extract_semantic_role_signals("):]
    body = body[:body.index("ALIGNMENT_ACTIVE_SET_MAX")]
    assert "except Exception" in body and "return {}" in body
    assert "CQ_ROLE_SEMANTICS_ENABLED" in body


def test_a_reingest_keeps_the_larger_reading_and_never_sums():
    """One meeting read twice is still one meeting (doc 19.4), and a
    lane that runs no semantic pass must not erase one that did."""
    stmt = WORKER[WORKER.index("INSERT INTO person_appearances"):]
    stmt = stmt[:stmt.index('"""')]
    for col in ("follow_ups_assigned", "follow_ups_accepted", "agenda_moves",
                "upstream_deferrals", "directive_turns", "responsive_turns"):
        assert f"{col}," in stmt, "column is written at all"
        block = stmt[stmt.index(f"{col} = CASE"):]
        block = block[:block.index("END")]
        assert f"WHEN EXCLUDED.{col} IS NULL THEN person_appearances.{col}" in block
        assert "GREATEST" in block


def test_there_is_exactly_one_definition_of_what_a_turn_is():
    """Learned while writing this call, and it is rule 7 again. The turn
    loop existed TWICE, byte identical, inside `question_attribution` and
    `meeting_role_signals`. An edit aimed at the second landed in the
    first, and the only reason it was caught in minutes rather than in
    prod is that the two copies then returned tuples of different widths
    and 26 tests went red at once. A third private copy for this call
    would have been the third chance to make that mistake, so `_TURN` is
    walked in exactly one place and every caller reads that one parse.
    Same argument as `speaker_labels_in` deriving from
    `speaker_turn_counts`: one parser, one placeholder gate."""
    src = (ROOT / "src" / "contextquilt" / "services" / "extraction_schema.py").read_text()
    assert src.count("_TURN.finditer") == 1, "one walk of the transcript, not two"
    assert src.count("turns, self_key = transcript_turns(") == 2, (
        "both signal functions read the shared parse"
    )


def test_the_columns_are_nullable_so_unknown_stays_expressible():
    for col in ("follow_ups_assigned", "follow_ups_accepted", "agenda_moves",
                "upstream_deferrals", "directive_turns", "responsive_turns"):
        assert f"ADD COLUMN IF NOT EXISTS {col}" in MIGRATION
    body = MIGRATION.split("ALTER TABLE")[1].split("COMMENT")[0]
    assert "NOT NULL" not in body


def test_the_migration_says_a_zero_here_is_weaker_than_the_false_next_door():
    """Migration 42's FALSE is an exact parse. A zero here is a model
    failing to find something, which can be a miss, and a surface that
    reads it as "they never do this" is asserting what nobody observed."""
    low = MIGRATION.lower()
    assert "weaker" in low
    assert "not a refusal" in low or "not a refusal count" in low
    assert "is not turn_count" in low
