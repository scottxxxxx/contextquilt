"""Presence follows a speaker reassignment (docs/architecture/16-people.md 6.2a).

A null `last_present_at` has to MEAN "not present", or the client keeps
a local speaker-label fallback to cover CQ's ambiguity, and that local
index is not trustworthy (this app has a case of a stranger wearing the
user's enrolled name for a whole meeting). These guards pin the two
halves: the rule the reassign route writes by, and the rename route's
in-place branch that already preserves presence for free.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from contextquilt.services.person_appearances import (  # noqa: E402
    SPEAKER_METRICS,
    plan_speaker_map,
    reassignment_presence,
    reassignment_presence_target,
)

MAIN = (ROOT / "src" / "main.py").read_text()
DOC16 = (ROOT / "docs" / "architecture" / "16-people.md").read_text()


def _reassign() -> str:
    m = re.search(
        r'@app\.post\("/v1/quilt/\{user_id\}/reassign-speaker".*?'
        r"# Speaker map:",
        MAIN, re.DOTALL,
    )
    assert m, "reassign-speaker handler not found"
    return m.group(0)


def _fn(name: str) -> str:
    """One module-level function's source, by name."""
    m = re.search(
        rf"\nasync def {name}\(.*?(?=\n@app\.|\nasync def |\nclass |\ndef )",
        MAIN, re.DOTALL,
    )
    assert m, f"{name} not found"
    return m.group(0)


def _speaker_map() -> str:
    m = re.search(
        r'@app\.post\("/v1/quilt/\{user_id\}/speaker-map".*?'
        r"# People . identity write-back",
        MAIN, re.DOTALL,
    )
    assert m, "speaker-map handler not found"
    return m.group(0)


def _rename() -> str:
    m = re.search(
        r'@app\.post\("/v1/quilt/\{user_id\}/rename-speaker".*?class FromLabel',
        MAIN, re.DOTALL,
    )
    assert m, "rename-speaker handler not found"
    return m.group(0)


# --- the rule ---------------------------------------------------------

def test_a_label_that_moved_nothing_records_no_presence():
    """The request named the label; the meeting never used it. Nothing
    was observed, so nothing is asserted."""
    assert reassignment_presence(
        [{"origin_id": "m1", "patches_moved": 0, "turn_count": 7}]
    ) == []


def test_moved_utterances_record_one_presence_per_meeting():
    writes = reassignment_presence([
        {"origin_id": "m2", "patches_moved": 3, "turn_count": None},
        {"origin_id": "m1", "patches_moved": 1, "turn_count": None},
    ])
    assert [w["origin_id"] for w in writes] == ["m1", "m2"], "ordered, so writes are stable"


def test_two_labels_in_one_meeting_stay_one_appearance_with_max_turns():
    """The merge route's rule verbatim: one human wearing two labels,
    41 turns and 1 turn, is a 41-turn human and one meeting."""
    writes = reassignment_presence([
        {"origin_id": "m1", "patches_moved": 2, "turn_count": 41},
        {"origin_id": "m1", "patches_moved": 1, "turn_count": 1},
    ])
    assert writes == [{"origin_id": "m1", "turn_count": 41}]


def test_unknown_turns_never_clobber_a_known_count():
    """Migration 34: NULL is unknown, never "spoke zero turns"."""
    both_orders = [
        reassignment_presence([
            {"origin_id": "m1", "patches_moved": 1, "turn_count": None},
            {"origin_id": "m1", "patches_moved": 1, "turn_count": 12},
        ]),
        reassignment_presence([
            {"origin_id": "m1", "patches_moved": 1, "turn_count": 12},
            {"origin_id": "m1", "patches_moved": 1, "turn_count": None},
        ]),
    ]
    assert both_orders[0] == both_orders[1] == [{"origin_id": "m1", "turn_count": 12}]


def test_no_turn_count_anywhere_stays_null():
    assert reassignment_presence(
        [{"origin_id": "m1", "patches_moved": 4, "turn_count": None}]
    ) == [{"origin_id": "m1", "turn_count": None}]


# --- to_self ----------------------------------------------------------

def test_to_person_presence_lands_on_that_person():
    assert reassignment_presence_target("person-1", False, "ego-1") == "person-1"


def test_to_self_presence_lands_on_the_ego_entity():
    """The ego is a People row like any other, carrying the same signals
    block. Skipping it would leave the user's own row as the one place a
    null anchor still means cannot-tell."""
    assert reassignment_presence_target(None, True, "ego-1") == "ego-1"


def test_to_self_with_no_ego_link_writes_nothing():
    """Migration 35 is keep-first because a moving ego reshapes every
    graph read. A route about speaker attribution does not get to decide
    who the user is, so absent link means record nothing."""
    assert reassignment_presence_target(None, True, None) is None


# --- the route wiring -------------------------------------------------

def test_presence_is_written_with_speaker_capacity():
    src = _fn("_upsert_speaker_appearance")
    assert "INSERT INTO person_appearances" in src
    assert "ARRAY['speaker']" in src
    assert "_upsert_speaker_appearance" in _reassign(), "the reassign path writes it"


def test_presence_is_dated_by_the_meeting_never_by_now():
    """The defect this came from: a label fixed today rendering as "Last
    met 6 hours ago" for a meeting three days old. The anchor is the
    meeting's ingest clock, from its sibling appearances or its own
    patches, and a meeting with neither is skipped rather than dated."""
    anchors = _fn("_meeting_presence_anchors")
    assert "MIN(first_seen_at) AS anchor" in anchors
    assert "MIN(cp.created_at) AS anchor" in anchors
    insert = _fn("_upsert_speaker_appearance")
    insert = insert[insert.index("INSERT INTO person_appearances"):]
    insert = insert[:insert.index("ON CONFLICT")]
    assert "NOW()" not in insert
    assert "if anchor is None:" in _reassign()


def test_presence_upsert_follows_the_merge_route_discipline():
    src = _fn("_upsert_speaker_appearance")
    upsert = src[src.index("ON CONFLICT (user_id, entity_id, origin_id)"):]
    assert "LEAST(person_appearances.first_seen_at" in upsert
    assert "GREATEST(person_appearances.last_seen_at" in upsert
    assert "SELECT DISTINCT unnest(" in upsert, "capacities union, never replace"
    assert "WHEN EXCLUDED.turn_count IS NULL THEN person_appearances.turn_count" in upsert


def test_the_source_labels_appearance_is_left_alone():
    """A reassignment says whose voice it was, not that nobody was
    there. Deleting the source row would assert an absence from the same
    evidence that proved a presence, and would take turn and question
    counts that can never be reconstructed."""
    src = _reassign()
    assert "DELETE FROM person_appearances" not in src
    assert "UPDATE person_appearances" not in src


def test_the_target_is_never_deleted_by_the_label_cleanup():
    """A from_label carrying the target's own name would otherwise drop
    the person just reassigned to, cascading away the appearance."""
    src = _reassign()
    cleanup = src[src.index("DELETE FROM entities"):]
    assert "($3::uuid IS NULL OR entity_id <> $3::uuid)" in cleanup


def test_a_suppressed_target_accumulates_no_meeting_history():
    """"Not a person" stops the row counting meetings, the same rule the
    ingest path enforces in worker._record_appearance."""
    src = _reassign()
    assert "suppressed_at" in src
    assert re.search(r"None if target_row\[.suppressed_at.\] is not None", src)


def test_the_ego_lookup_is_the_stamped_link_and_not_a_name_guess():
    src = _reassign()
    assert re.search(
        r"SELECT entity_id FROM entities .*self_at IS NOT NULL", src, re.DOTALL
    )
    assert "self_at = NOW()" not in src, "this route never mints an ego stamp"


def test_presence_lands_on_the_canonical_after_a_merge():
    """The People list reads merged_into IS NULL, so presence written to
    a folded row is presence nobody can see."""
    assert "COALESCE(merged_into, entity_id) AS presence_entity_id" in _reassign()


def test_the_response_ships_the_count_and_where_it_landed():
    src = _reassign()
    assert '"appearances_recorded": appearances_recorded' in src
    assert '"presence_entity_id"' in src


# --- rename-speaker: what is preserved, and what cannot be ------------

def test_rename_in_place_keeps_the_entity_id_so_appearances_follow():
    """person_appearances is keyed on entity_id. The existing-entity
    branch renames the row in place, which is why presence survives for
    free. Nobody may "fix" this into a delete plus recreate."""
    src = _rename()
    assert "UPDATE entities SET name = $1" in src
    assert "DELETE FROM entities" not in src
    for sql in ("INSERT INTO person_appearances",
                "UPDATE person_appearances",
                "DELETE FROM person_appearances"):
        assert sql not in src, "the in-place rename needs no appearance work at all"


def test_rename_documents_the_placeholder_branch_it_cannot_fix():
    """The created-entity branch has no meeting id in the request, so no
    appearance can honestly be attached. Stated, not papered over."""
    src = _rename()
    assert "PRESENCE, AND WHERE IT CANNOT FOLLOW" in src
    assert "no meeting id" in src
    assert "16-people.md 6.2a" in src


def test_doc_16_names_every_remaining_ambiguous_null():
    assert "### 6.2a" in DOC16
    assert "rename-speaker" in DOC16


# --- the state sync: plan_speaker_map ---------------------------------
#
# A tiny model of person_appearances, so the plan can be APPLIED and
# re-planned. Idempotency is a hard requirement here, not a nice
# property, and asserting it needs a second pass over real state.

def _row(eid, caps, **metrics):
    r = {"entity_id": eid, "capacities": list(caps),
         "first_seen_at": "T0", "last_seen_at": "T0"}
    r.update({m: None for m in SPEAKER_METRICS})
    r.update(metrics)
    return r


def _apply(rows, plan, anchor="T9"):
    """What the route's SQL does, in memory."""
    by_id = {r["entity_id"]: r for r in rows}
    for eid in plan["add"]:
        row = by_id.get(eid)
        if row is None:
            # A new row is dated by the MEETING anchor.
            by_id[eid] = _row(eid, ["speaker"])
            by_id[eid]["first_seen_at"] = by_id[eid]["last_seen_at"] = anchor
        else:
            row["capacities"] = sorted(set(row["capacities"]) | {"speaker"})
    for eid in plan["strip"]:
        row = by_id[eid]
        row["capacities"] = sorted(set(row["capacities"]) - {"speaker"})
        for m in SPEAKER_METRICS:
            row[m] = None
    for eid in plan["remove"]:
        by_id.pop(eid)
    return list(by_id.values())


def test_a_speaker_the_mapping_adds_gets_an_appearance():
    plan = plan_speaker_map([], {"p1"})
    assert plan == {"add": ["p1"], "strip": [], "remove": []}


def test_an_existing_speaker_needs_no_write():
    """The property idempotency rests on: only necessary work is planned."""
    plan = plan_speaker_map([_row("p1", ["speaker"])], {"p1"})
    assert plan == {"add": [], "strip": [], "remove": []}


def test_a_speaker_only_row_the_mapping_drops_is_removed():
    """Nothing but the label ever claimed this person was in the room."""
    plan = plan_speaker_map([_row("p1", ["speaker"])], set())
    assert plan == {"add": [], "strip": [], "remove": ["p1"]}


def test_a_row_standing_on_another_capacity_survives_the_drop():
    """The case that makes removal safe: an owner was in that meeting
    whether or not a label still points at them, so the appearance keeps
    its presence and loses only the speaker claim."""
    plan = plan_speaker_map([_row("p1", ["ownership", "speaker"])], set())
    assert plan == {"add": [], "strip": ["p1"], "remove": []}


def test_a_mention_only_row_is_never_touched():
    """The mapping speaks about speaker labels. It may not add or remove
    a claim of a grade it never described."""
    assert plan_speaker_map([_row("p1", ["mention"])], set()) == {
        "add": [], "strip": [], "remove": []
    }


def test_an_empty_capacities_row_is_never_touched():
    """Empty is pre-migration-31 UNKNOWN, not "speaker", and unknown
    already counts as presence. Deleting it would be inference."""
    assert plan_speaker_map([_row("p1", [])], set()) == {
        "add": [], "strip": [], "remove": []
    }


def test_an_unresolved_label_costs_the_whole_removal_half():
    """Removal works by absence, which is only sound against a complete
    target set. One label CQ could not resolve and absence stops meaning
    "did not speak"."""
    rows = [_row("p1", ["speaker"]), _row("p2", ["speaker"])]
    plan = plan_speaker_map(rows, {"p1"}, allow_removal=False)
    assert plan == {"add": [], "strip": [], "remove": []}


def test_the_same_mapping_twice_changes_nothing_the_second_time():
    """Idempotency, end to end over the model: an undo is just the
    post-undo mapping, and a lane that fires twice must not double
    anything or move a timestamp."""
    rows = [
        _row("speaker_label", ["speaker"], turn_count=41, questions_asked=3),
        _row("owner_only", ["ownership", "speaker"], turn_count=2),
        _row("mentioned", ["mention"]),
    ]
    mapping = {"priya"}

    first = plan_speaker_map(rows, mapping)
    assert first == {"add": ["priya"], "strip": ["owner_only"],
                     "remove": ["speaker_label"]}
    after = _apply(rows, first)

    second = plan_speaker_map(after, mapping)
    assert second == {"add": [], "strip": [], "remove": []}, "not idempotent"

    unchanged = _apply(after, second)
    assert {r["entity_id"] for r in unchanged} == {"priya", "owner_only", "mentioned"}
    for r in unchanged:
        # No timestamp moved on the second pass.
        assert (r["first_seen_at"], r["last_seen_at"]) in {("T0", "T0"), ("T9", "T9")}
    survivor = next(r for r in unchanged if r["entity_id"] == "owner_only")
    assert survivor["capacities"] == ["ownership"]
    assert all(survivor[m] is None for m in SPEAKER_METRICS), (
        "a per-speaker measurement follows the label that produced it"
    )
    mention = next(r for r in unchanged if r["entity_id"] == "mentioned")
    assert mention["capacities"] == ["mention"]


def test_the_undo_case_restores_the_prior_state():
    """Relabel then undo: the post-undo mapping names nobody, and the
    appearance the relabel created goes away again."""
    rows = []
    after_relabel = _apply(rows, plan_speaker_map(rows, {"priya"}))
    assert [r["entity_id"] for r in after_relabel] == ["priya"]
    after_undo = _apply(after_relabel, plan_speaker_map(after_relabel, set()))
    assert after_undo == []


# --- the state sync: route wiring -------------------------------------

def test_speaker_map_route_exists():
    assert '@app.post("/v1/quilt/{user_id}/speaker-map"' in MAIN


def test_a_partial_mapping_is_refused_rather_than_deleting():
    """Removal by absence needs the caller to have sent every label, and
    CQ cannot verify that. A half-wired lane must fail loudly."""
    src = _speaker_map()
    assert "INCOMPLETE_MAPPING" in src
    assert "if not req.labels_are_complete" in src


def test_nobody_is_explicit_never_inferred_from_an_empty_entry():
    """This is the field that removes a presence. A client that forgot to
    fill in a target gets a 422, not a deletion."""
    src = _speaker_map()
    assert "to_nobody" in src
    assert "INVALID_LABEL_TARGET" in src


def test_the_mapping_never_rewrites_ownership_or_text():
    """Speaking and owning are different claims: work gets assigned in
    absentia, so a map of who spoke must not re-own commitments."""
    src = _speaker_map()
    assert "UPDATE context_patches" not in src
    assert "'{owner}'" not in src


def test_stripping_clears_the_per_speaker_measurements():
    src = _speaker_map()
    assert "array_remove(capacities, 'speaker')" in src
    assert "= NULL" in src


def test_the_mapping_dates_additions_by_the_meeting():
    src = _speaker_map()
    assert "_meeting_presence_anchors" in src
    assert "if anchor is not None:" in src


def test_speaker_metrics_exclude_the_meeting_level_denominator():
    """meeting_questions_by_user counts what the USER asked in the
    meeting. It is not a claim about this person and must not be nulled
    when a label moves."""
    assert "meeting_questions_by_user" not in SPEAKER_METRICS
    assert "turn_count" in SPEAKER_METRICS
    assert len(SPEAKER_METRICS) == 6
