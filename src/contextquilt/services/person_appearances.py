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
"""

# The vocabulary. Ordered strongest to weakest as provenance, which is the
# order the backfill's tiers run in.
OWNERSHIP = "ownership"
SPEAKER = "speaker"
MENTION = "mention"

CAPACITIES = (OWNERSHIP, SPEAKER, MENTION)


def counts_as_attendance(capacities) -> bool:
    """True unless we positively know this appearance was mention-only.

    `meeting_count` renders to a user as "9 meetings", a claim about being
    THERE. Being named in a transcript is not attendance: someone can be
    discussed at length in a meeting they never joined. On prod, counting
    mentions took the busiest person from 37 meetings to 179.

    Stated as an exclusion rather than an inclusion, deliberately. An
    appearance counts unless its capacity set is exactly {mention}, so
    ownership, speaker, both, and UNKNOWN all count. That follows the rule
    in migration 31: a row carrying no capacity means we do not know, not
    that the person was absent, and silently dropping unknowns from a
    number a user reads would be inventing a claim we cannot support.

    The practical consequence is that turning the mention tier on cannot
    move any existing number, because no row today is mention-only. If a
    count changes on the day this ships, this predicate is wrong.
    """
    caps = set(capacities or ())
    return caps != {MENTION}


def counts_as_named(capacities) -> bool:
    """True when the person was named in this meeting's transcript.

    Feeds the provenance line ("named in 11 transcripts"), which is a
    different and larger number than attendance on purpose. Someone who
    spoke was also named, so this deliberately overlaps attendance rather
    than partitioning against it.
    """
    return MENTION in set(capacities or ())


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
