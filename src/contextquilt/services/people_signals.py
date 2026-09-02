"""Per-person list signals (design 17a): the numbers behind the
situation sections, served so every client renders the same truth.

The 17a list assigns people to situations (UP NEXT, OPEN BETWEEN YOU,
DRIFTING, NEW FACES) and writes one sentence per row. CQ serves the
HONEST INPUTS, computed once here from data the list assembly already
holds; the client owns the situation assignment and the sentence
(calendar and local meeting titles are theirs). House rules
throughout: null is cannot-tell, zero is measured, counts match the
lists they open, dates bucket to days.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from statistics import median
from typing import Dict, List, Optional

# A cadence needs history: below this many distinct meeting days the
# median interval is an anecdote and serves null.
CADENCE_MIN_MEETINGS = 4


def _day(ts) -> Optional[date]:
    if isinstance(ts, datetime):
        return ts.date()
    if isinstance(ts, date):
        return ts
    return None


def is_presence_grade(appearance: dict) -> bool:
    """True when an appearance row is evidence the person was THERE.

    A mention is not a meeting (the RV/Raj field report, 2026-08-11):
    someone NAMED in a room did not attend it, and "met 2 hours ago" is
    a presence claim. Presence grade = speaker or ownership capacity;
    EMPTY capacities are pre-migration-31 rows and count as presence per
    that migration's own rule (unknown must not become "did not
    attend"). Mention-only rows still exist on the person (the directory
    knows the name) but never feed a presence number.

    Module level so the commitment ledger's "meetings since this was
    last said" counts the SAME meetings this module's cadence does. Two
    definitions of presence would let one surface say a person has been
    met twice since an item went quiet while another says nothing has
    happened.
    """
    caps = set(appearance.get("capacities") or [])
    return not caps or bool(caps & {"speaker", "ownership"})


QUESTION_FIELDS = (
    "questions_asked",
    "questions_received_explicit",
    "questions_received_inferred",
    "questions_from_user_explicit",
    "questions_from_user_inferred",
)


def compute_question_totals(appearances: List[dict]) -> Dict:
    """One person's question counts across the meetings CQ measured.

    Captured per appearance at ingest (migration 37) because transcripts
    are derive-then-discard. What this is for: CQ can say what a person
    owes and how their items closed, and this is the other half, who the
    user actually presses for an answer. CQ serves both sets of counts
    and says nothing about how they line up. There is no ratio here, no
    score, and no served string naming a pattern; that reading belongs to
    the client, on numbers it can open.

    Two grades, never summed. `explicit` is a vocative CQ read in the
    transcript ("Marcus, can you get me that?"). `inferred` is a guess
    from who spoke next, and it is wrong sometimes. A client may render
    the explicit column alone; it could never separate them again if CQ
    added them together here.

    Every total carries its denominators: `meetings_measured` (how many
    of this person's meetings carried the metric at all, which is zero
    for every meeting that predates it) and `user_asked_total` (how many
    questions the user asked in those meetings, so two out of three and
    two out of forty do not render the same). Null everywhere means
    cannot tell, never zero asked.
    """
    measured = [
        a for a in appearances or ()
        if any(a.get(f) is not None for f in QUESTION_FIELDS)
    ]
    present = [a for a in appearances or () if is_presence_grade(a)]

    def _sum(field: str) -> Optional[int]:
        # Null per FIELD, not per row: a meeting can know how many
        # questions a person asked and still not know which speaker was
        # the user, and the from_user pair has to say so rather than
        # report a zero the transcript never supported.
        vals = [a.get(field) for a in measured if a.get(field) is not None]
        return sum(vals) if vals else None

    user_asked = [
        a.get("meeting_questions_by_user") for a in measured
        if a.get("meeting_questions_by_user") is not None
    ]
    return {
        "meetings_measured": len(measured),
        "meetings_present": len(present),
        "asked": _sum("questions_asked"),
        "received_explicit": _sum("questions_received_explicit"),
        "received_inferred": _sum("questions_received_inferred"),
        "from_user_explicit": _sum("questions_from_user_explicit"),
        "from_user_inferred": _sum("questions_from_user_inferred"),
        # The denominator for the from_user pair: every question the user
        # asked in the measured meetings, whoever it landed on. Null when
        # no meeting could identify which speaker is the user.
        "user_asked_total": sum(user_asked) if user_asked else None,
    }


def presence_anchor(appearances: List[dict]) -> Dict:
    """First/last day this person was actually PRESENT, plus the count.

    Split out of `compute_person_signals` so the person DETAIL route can
    serve the same anchor the person LIST serves, from the identical
    predicate over identical rows. Added 2026-08-15 after SS found their
    person page rendering the entity-level `last_seen_at` as "last met":
    that column is mention-inclusive AND is stamped to NOW() by a rename,
    so it could claim a meeting that never happened. They fixed the render
    to read the presence anchor, which the detail route did not serve, so
    a person opened by deep link had no anchor at all.

    Two implementations of "when did we last meet" is the drift this whole
    workstream has been retiring. There is one, and it lives here.
    """
    present = [a for a in appearances if is_presence_grade(a)]
    days = sorted({d for a in present if (d := _day(a.get("last_seen_at")))})
    return {
        # null = never actually present (mention-only person), which is a
        # different claim from "met long ago" and must render as one.
        "first_present_at": days[0].isoformat() if days else None,
        "last_present_at": days[-1].isoformat() if days else None,
        # DAYS, both keys. `meetings_present` here has always been
        # len(days), which is a different unit from the
        # `questions.meetings_present` served beside it on the same row
        # (that one is appearance rows). Same key, two units, one
        # surface: found 2026-09-01 in the audit SS's day-count report
        # triggered. `days_present` is the honest name; the old key keeps
        # serving until clients move.
        "meetings_present": len(days),
        "days_present": len(days),
    }


def compute_person_signals(
    appearances: List[dict],
    they_owe: List[dict],
    you_owe: Optional[List[dict]],
    today: Optional[date] = None,
) -> Dict:
    """The `signals` block for one person row.

    appearances: [{last_seen_at, turn_count}] (one per meeting, as the
    list assembly fetches them). they_owe / you_owe: the SAME item rows
    the ledger serves (shelved already excluded), so every count here
    agrees with the list it opens; you_owe None passes through as
    cannot-tell per the owed_to capability.
    """
    today = today or datetime.now(timezone.utc).date()

    # A mention is not a meeting: see is_presence_grade, which is now
    # module level so the commitment ledger counts the same meetings.
    present = [a for a in appearances if is_presence_grade(a)]
    present_days = sorted(
        {d for a in present if (d := _day(a.get("last_seen_at")))}
    )
    appearances = present
    days = present_days
    meetings_7d = sum(1 for d in days if (today - d).days < 7)
    meetings_30d = sum(1 for d in days if (today - d).days < 30)
    turns = [
        a.get("turn_count") for a in appearances
        if a.get("turn_count") is not None
        and (d := _day(a.get("last_seen_at"))) and (today - d).days < 30
    ]
    turns_30d = sum(turns) if turns else None

    cadence = None
    if len(days) >= CADENCE_MIN_MEETINGS:
        intervals = [(b - a).days for a, b in zip(days, days[1:]) if (b - a).days > 0]
        if intervals:
            cadence = {
                "median_interval_days": round(median(intervals)),
                "meetings_observed": len(days),
            }

    def _overdue(item) -> bool:
        dd = item.get("deadline_date")
        return bool(dd) and str(dd) < today.isoformat()

    def _summ(item, direction):
        v = item.get("value") or {}
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except (ValueError, TypeError):
                v = {}
        text = item.get("text") or v.get("text") or ""
        return {
            "direction": direction,
            "patch_id": str(item["patch_id"]) if item.get("patch_id") else None,
            "text": text,
            "deadline_date": item.get("deadline_date"),
            "overdue": _overdue(item),
        }

    candidates = [(i, "they_owe") for i in they_owe]
    if you_owe:
        candidates += [(i, "you_owe") for i in you_owe]
    # Most urgent first, deterministically: overdue beats dated beats
    # undated; earlier deadline beats later; patch id breaks ties.
    candidates.sort(key=lambda c: (
        not _overdue(c[0]),
        c[0].get("deadline_date") is None,
        str(c[0].get("deadline_date") or "9999"),
        str(c[0].get("patch_id") or ""),
    ))
    next_item = _summ(*candidates[0]) if candidates else None

    # THE UNIT IS DAYS. `present_days` is a set of dates, so every count
    # here is distinct calendar days on which the person was present,
    # and three meetings on one Tuesday are one Tuesday, which is the
    # right measure of a rhythm. The names `meetings_7d`, `meetings_30d`
    # and `cadence.meetings_observed` said otherwise, and SS found it on
    # 2026-09-01 from a device payload: six meetings listed above a tile
    # reading "4 MEETINGS, 30D". Doc 16 section 5.13: a served name may
    # assert only what was observed. The honest names are served
    # alongside with the SAME values; the old ones keep serving unchanged
    # until every client has moved, then retire. Additive, so no decoder
    # has to guess (and GP has to forward the new keys, not drop them).
    if cadence is not None:
        cadence = {**cadence, "days_observed": cadence["meetings_observed"]}
    return {
        "meetings_7d": meetings_7d,
        "meetings_30d": meetings_30d,
        "days_present_7d": meetings_7d,
        "days_present_30d": meetings_30d,
        "turns_30d": turns_30d,
        "cadence": cadence,
        # Presence anchors for "met X ago" copy and NEW FACES gating.
        # Same values `presence_anchor` serves on the person detail: one
        # implementation, so the two screens cannot disagree.
        **{
            k: v for k, v in presence_anchor(appearances).items()
            if k not in ("meetings_present", "days_present")
        },
        "open_between": {
            "they_owe_open": len(they_owe),
            "they_owe_overdue": sum(1 for i in they_owe if _overdue(i)),
            # Null when the caller's manifest lacks owed_to: cannot-tell,
            # never rendered as "you owe nothing".
            "you_owe_open": None if you_owe is None else len(you_owe),
            "next_open_item": next_item,
        },
    }


# ---------------------------------------------------------------
# last_seen_in: the one meeting worth a badge in a disambiguation list.
#
# Scott, 2026-08-23, labelling a live speaker "Sam": the picker offered
# "Sam Altman · 0 meetings" and "Sam Wisco · 1 meeting", and what would
# have settled it is WHERE Sam Wisco was last seen. `top_project` cannot
# do that job: it is presence-grade only (speaker/ownership), and Sam
# Wisco was merely mentioned, so it serves null for exactly the person
# who needs the badge. This field is the most recent appearance in ANY
# capacity, and it says which capacity, because "mentioned in Agent
# Utilization" and "spoke in Agent Utilization" are different claims
# and a served name may assert only what was observed (doc 16 5.13).
# ---------------------------------------------------------------

def last_seen_in(appearances: list) -> "dict | None":
    """The newest appearance, any capacity, as a badge-sized object.

    {project_id, project, origin_id, last_seen_at, capacities}. `project`
    and `project_id` are null when that meeting had no project; the
    object is still served so the date and capacity carry. None when the
    person has no appearances at all (mention-only people who predate
    migration 31 have none; that is "0 meetings" and stays honest).
    Empty capacities are pre-31 rows and are served as-is, never
    fabricated into "speaker".
    """
    best = None
    best_key = None
    for a in appearances or []:
        ts = a.get("last_seen_at")
        key = ts.isoformat() if hasattr(ts, "isoformat") else (ts or "")
        if best is None or key > best_key:
            best, best_key = a, key
    if best is None:
        return None
    ts = best.get("last_seen_at")
    return {
        "project_id": best.get("project_id"),
        "project": best.get("project"),
        "origin_id": best.get("origin_id"),
        "last_seen_at": ts.isoformat() if hasattr(ts, "isoformat") else ts,
        "capacities": sorted(best.get("capacities") or []),
    }


# The day `owed_to` edges became producible. Before this, the edge shape
# was carried only by a JSON schema the Anthropic client does not put on
# the wire, so the model invented keys and every edge was discarded on
# arrival. Two survived in three months across 9,088 connections.
OWED_TO_OBSERVABLE_SINCE = datetime(2026, 9, 1, tzinfo=timezone.utc)


def owed_to_instrument_has_looked(open_items: List[dict],
                                  completable_types,
                                  is_self_owned,
                                  since: datetime = OWED_TO_OBSERVABLE_SINCE) -> bool:
    """Whether an EMPTY `you_owe` would be an observation or a guess.

    `[]` asserts "you owe this person nothing", and that is only honest
    when EVERY open item the user owns was captured by a working
    instrument. One unobservable item is enough to make the claim a
    guess, because that item might be the one owed to this person.

    The first version of this gate asked only whether the instrument
    had looked at SOMETHING. Five commitments extracted the day of the
    fix satisfied it, and Scott's card went on saying "nothing open"
    above two items it could never see, among 601 more. Looking at some
    of the evidence does not license a claim about all of it.

    An item is observable when it was extracted (it carries an origin)
    on or after the fix. A hand-written item never is: the client
    composer sends no owed_to, so it could never carry the edge and
    cannot vouch for absence. That means a user who writes items by
    hand gets null here until the composer asks who the item is for,
    which is the honest state rather than a defect.

    A real edge is always served regardless; this only decides between
    `[]` and null when there is none. True only when there is at least
    one observable item AND no unobservable one.
    """
    observable = 0
    for r in open_items or ():
        if r.get("patch_type") not in completable_types:
            continue
        if not is_self_owned(r.get("owner")):
            continue
        created = r.get("created_at")
        if r.get("origin_id") and created is not None and created >= since:
            observable += 1
        else:
            # One item the instrument could not see is enough.
            return False
    return observable > 0
