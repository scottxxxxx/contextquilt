"""Appearance capacity: how a person showed up in a meeting.

`person_appearances` records one row per (person, meeting). Migration 31
adds the capacity in which they appeared, because THAT they appeared is
not enough to use co-appearance as an identity signal.

The concrete failure it fixes, measured on prod 2026-08-03: of the 16
candidate name-variant pairs, the four that co-occur all look like the
SAME person, typically a short speaker label plus a fuller name someone
said out loud in the same room. A co-occurrence veto with no capacity
would have blocked every true merge it saw.

Absence is not evidence of absence. Speaker and mention capacities come
from retained transcripts, and retention is bounded. A row carrying no
capacity means unknown, never "not in that capacity", so any veto built on
this must require positive evidence from both sides.

Also here: the pure half of "presence follows a reassignment"
(`reassignment_presence*`), so the rules the reassign-speaker route
writes by are testable without a database.
"""

from typing import Dict, List, Optional

# The vocabulary. Ordered strongest to weakest as provenance, which is the
# order the backfill's tiers run in.
OWNERSHIP = "ownership"
SPEAKER = "speaker"
MENTION = "mention"

CAPACITIES = (OWNERSHIP, SPEAKER, MENTION)


def merge_tier(rows: dict, found: dict, capacity: str) -> int:
    """Fold one tier's findings into the accumulator.

    Two different jobs, and conflating them is what the old short-circuit
    did. The FIRST tier to find a (person, meeting) still wins on
    timestamps and project scope, because the cheaper and more certain
    signal is the better provenance. But capacity ACCUMULATES, so a person
    recorded by ownership who also spoke ends up carrying both.

    The previous version skipped keys an earlier tier had already found,
    which meant the strongest capacity silently masked every other one.
    That is precisely why co-appearance could not tell two speakers apart
    from one speaker whose full name someone said aloud.

    Copies each row rather than tagging it in place, so a caller may reuse
    a tier's result. Returns how many keys this tier contributed that were
    not already present.
    """
    new = 0
    for key, row in found.items():
        existing = rows.get(key)
        if existing is None:
            row = dict(row)
            row["capacities"] = {capacity}
            rows[key] = row
            new += 1
        else:
            existing.setdefault("capacities", set()).add(capacity)
    return new


def reassignment_presence_target(
    target_entity_id, to_self: bool, self_entity_id,
):
    """Which entity a speaker reassignment's presence lands on, or None.

    `to_person_id` lands on that person. `to_self` lands on the EGO
    ENTITY (migration 35) and nowhere else.

    The ego is not exempt from presence. It is a row on the People list
    like any other, carrying the same `signals` block with the same
    `last_present_at`, so leaving it out would make the user's own row
    the one place a null anchor still means "cannot tell". The user
    saying "those utterances were me" is the same grade of evidence
    about presence as saying they were Marcus.

    What the ego DOES get is a harder guard: no ego link, no write, and
    never a stamp minted here. The ego link is keep-first by design
    (migration 35: a moving ego silently reshapes every graph read), so
    a route about speaker attribution has no business deciding who the
    user is. Absent link returns None and the caller records nothing
    rather than guessing at a target.
    """
    if to_self:
        return self_entity_id
    return target_entity_id


def reassignment_presence(outcomes: List[dict]) -> List[dict]:
    """One presence write per meeting a reassignment actually moved.

    `outcomes` is one entry per (label, meeting) the caller processed:
    `{origin_id, patches_moved, turn_count}`, where `turn_count` is the
    SOURCE label's recorded turns for that meeting (None when unknown).

    Two rules, both about asserting only what was observed:

    * A label that moved NOTHING produces no presence. The request named
      it, but nothing in the meeting was ever attributed to it, so there
      is no evidence anybody spoke under it.
    * Several labels folding into one person in one meeting stay ONE
      appearance, because that is still one meeting, and the turn count
      is the MAX rather than the sum. That is the merge route's rule
      verbatim (one human wearing two labels, 41 turns and 1 turn, is a
      41-turn human), and the two paths make the same claim about the
      same kind of diarization split, so they must not disagree.

    Returns `[{origin_id, turn_count}]`, ordered by origin_id so a
    request with the same content always writes in the same order.
    """
    by_meeting: Dict[str, Optional[int]] = {}
    for o in outcomes or ():
        if not o.get("patches_moved"):
            continue
        origin_id = o.get("origin_id")
        if not origin_id:
            continue
        turns = o.get("turn_count")
        prior = by_meeting.get(origin_id, "absent")
        if prior == "absent":
            by_meeting[origin_id] = turns
        elif turns is not None:
            # NULL never clobbers a known count (migration 34: NULL is
            # unknown, never "spoke zero turns").
            by_meeting[origin_id] = turns if prior is None else max(prior, turns)
    return [
        {"origin_id": oid, "turn_count": by_meeting[oid]}
        for oid in sorted(by_meeting)
    ]


# Per-speaker measurements, captured at ingest from a transcript label
# and true of that LABEL's speech. `meeting_questions_by_user` is
# deliberately absent: it counts what the user asked in the meeting, a
# property of the meeting rather than a claim about this person.
SPEAKER_METRICS = (
    "turn_count",
    "questions_asked",
    "questions_received_explicit",
    "questions_received_inferred",
    "questions_from_user_explicit",
    "questions_from_user_inferred",
)


def plan_speaker_map(rows: List[dict], target_entity_ids, allow_removal: bool = True) -> Dict:
    """Diff a meeting's declared speaker set against the appearances held.

    The answer to "what is the inverse of a reassignment", which has no
    answer as asked: an undo can mean "it was never him" (the appearance
    is false) or "put the raw labels back on screen" (the appearance is
    true and reverting it destroys a fact), and no operation-keyed rule
    can tell those apart. So nothing here is keyed on an operation. The
    caller states the resulting STATE, who spoke in this meeting as it
    now stands, and this plans the difference. An undo is just the
    post-undo state; a block-scoped edit is just the post-edit state, so
    segment ranges are never modelled; a consolidation is the same state
    with one fewer speaker.

    `rows` are the meeting's current `person_appearances`, each with
    `entity_id` and `capacities`. `target_entity_ids` is who the mapping
    now says spoke.

    Returns `{"add": [...], "strip": [...], "remove": [...]}`, all lists
    of entity_id, all sorted:

    * `add`     upsert a speaker-capacity appearance (missing row, or a
                row that does not yet carry `speaker`).
    * `strip`   the row loses `speaker` and its SPEAKER_METRICS, but
                SURVIVES, because it stands on another capacity. This is
                the case that makes removal safe: a person recorded by
                ownership was in that meeting whether or not a label
                still points at them, and dropping the appearance would
                delete a presence the mapping never spoke about.
                Capacities are a set precisely so this is expressible.
    * `remove`  `speaker` was the row's ONLY capacity, so the whole row
                goes. Nothing else ever claimed this person was there.

    An EMPTY capacities row is left alone in both directions. Empty means
    pre-migration-31 unknown, not "speaker", and a mapping about speaker
    labels may not quietly delete a presence it cannot see the grade of.

    `allow_removal=False` plans additions only. The caller passes it when
    any label failed to resolve, because removal by absence is only sound
    against a COMPLETE target set: one unresolved label and absence stops
    meaning "did not speak".

    IDEMPOTENT BY CONSTRUCTION, which is the property that makes this
    safe on lanes nobody has found yet. Only necessary work is planned,
    so applying a plan and re-planning the same mapping yields an empty
    plan: no write, no timestamp moved, nothing to undo.
    """
    targets = {str(t) for t in (target_entity_ids or ()) if t}
    by_entity = {str(r["entity_id"]): r for r in (rows or ())}

    add = [
        eid for eid in sorted(targets)
        if SPEAKER not in set((by_entity.get(eid) or {}).get("capacities") or ())
    ]

    strip: List[str] = []
    remove: List[str] = []
    if allow_removal:
        for eid in sorted(by_entity):
            if eid in targets:
                continue
            caps = set(by_entity[eid].get("capacities") or ())
            if SPEAKER not in caps:
                continue
            (strip if caps - {SPEAKER} else remove).append(eid)

    return {"add": add, "strip": strip, "remove": remove}


def both_spoke(rows: dict, user_id: str, entity_a, entity_b, origin_id) -> bool:
    """True only on positive evidence that both parties spoke in a meeting.

    The identity veto. Deliberately returns False for unknown rather than
    treating a missing capacity as "did not speak", so a thin or aged-out
    transcript produces no veto instead of a false one.
    """
    def spoke(eid) -> bool:
        row = rows.get((user_id, eid, origin_id)) or {}
        return SPEAKER in (row.get("capacities") or set())

    return spoke(entity_a) and spoke(entity_b)
