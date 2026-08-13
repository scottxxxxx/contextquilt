"""Closure mode: how an item actually left the conversation, not whether
it is open.

An action item tracker answers one question, open or closed, and that
question is blind to the failure mode that matters most. Some items are
never failed, they are MOLTED: the same object comes back at the next
meeting as a differently shaped fresh commitment, the language stays
strong, and the state never changes. Motion reads as progress at every
checkpoint, which is exactly why it survives every accountability system
a user already has. It is visible only by holding one object across
months, which is the one thing CQ can do and a task list cannot.

The real shape, from transcripts: one thread ran "Yeah, absolutely, end
of next week, easy", then "So, partial, I have been trying to get on
Renata's calendar", then "I have a request in with legal, honestly?
Weeks." Three meetings, three commitments, zero state change. A second
ran "That is a good question", then "Let me think about who the right
person is", then "I have a shortlist". Note what degrades. The
commitment LANGUAGE holds; the OBJECT regresses, from a name, to a
person to identify, to a shortlist that still needs cleaning up.

What this module will and will not say:

- SHIP THE COUNT, NEVER THE CAUSE. "Six items assigned to this person
  have each been restated rather than closed, median three hops, zero
  closures" is a fact the user can act on and the subject can check.
  "He avoids accountability" is a verdict, it is unfalsifiable, and
  nothing here may emit it. Every mode name below describes what
  happened to an ITEM. None of them describes a person.
- STORE INSTANCES, NEVER TRAITS. Every count carries the patch ids
  behind it (`patch_ids_by_mode`), so a count is always openable into
  the dated, quoted items that produced it.
- NO PERCENTAGE ON A DENOMINATOR UNDER FIVE. Twenty meetings a month
  across twenty people is one to four observations per person per month,
  so most ratios are statistically empty at thirty days. This module
  serves no ratio at all: counts, and the denominator (`items`) next to
  them, so the client can refuse to render.

Pure functions, no database, no clock of its own (`today` is passed):
the same discipline `follow_through.py` runs on, and for the same
reason, every threshold here is unit testable without a stack.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from statistics import median
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .people_identity import is_self_owned
from .people_signals import is_presence_grade

# The closure modes. Exactly one of them is delivery. Open vocabulary
# downstream, the same rule `decay_state` carries: a client that does not
# know a mode passes it through, it never guesses at it.
DELIVERED = "delivered"
RE_DATED = "re_dated"
RESTATED = "restated"
REASSIGNED = "reassigned"
ABSORBED_BY_USER = "absorbed_by_user"
# Named for what was OBSERVED, not for what it probably means. It was
# `silently_dropped` until review, and that name was a verdict about the
# world while the evidence only supports a claim about the conversation.
#
# Every other mode here is self-evidencing: the receipt is the person's
# own words, in a meeting, on a date. This is the only one where ABSENCE
# does the work, and absence is the one thing a meeting cannot see. The
# item may have been finished by email on the Tuesday and never mentioned
# again, and CQ would hold exactly the same evidence either way. So the
# name says the observation ("this has not come up since") and the item
# carries `meetings_since_last_statement`, which lets a client render
# "has not come up in your last 3 meetings with her" and be exactly right
# whatever happened offline.
NOT_RAISED_SINCE = "not_raised_since"
OPEN = "open"

# Precedence, highest first, and the tie break is deliberate rather than
# alphabetical. An item can satisfy several of these at once (a re-dated
# item that then changed hands, a restated item nobody has raised in
# three meetings), so one of them has to be the headline.
#
# Ownership modes outrank the rest because a change of hands is a
# structural fact about the item that is true forever, while
# not_raised_since and re_dated are statements about the last few weeks.
# `not_raised_since` outranks `re_dated` for the same reason in the other
# direction: a re-date is the item being managed against a calendar, and
# the point of not_raised_since is that it stopped being managed OUT
# LOUD. Everything an item also qualifies for is served alongside in
# `modes`, so nothing is hidden by the choice.
MODE_PRECEDENCE = (
    DELIVERED,
    ABSORBED_BY_USER,
    REASSIGNED,
    NOT_RAISED_SINCE,
    RE_DATED,
    RESTATED,
    OPEN,
)

# Meetings with THAT PERSON, not elapsed days. A fortnight of silence
# says nothing when the two of them did not meet; two meetings where the
# item never came up is the thing the user cannot see for themselves.
# Two is the smallest number where "it did not come up" is a pattern
# rather than one crowded agenda. A parameter rather than a constant so
# it can be tuned against real data instead of re-argued.
MIN_MEETINGS_NOT_RAISED = 2

# How many restatement receipts travel with one item. Matches the write
# path's own cap, so in practice this truncates nothing; it exists so a
# hand-written or backfilled value cannot inflate one item's payload.
RESTATEMENT_RECEIPT_CAP = 10

# Summary keys that carry patch ids rather than counts. Named as a set so
# a surface that serves counts only (the person LIST, a browse surface)
# strips the family rather than one key it happens to know about: adding
# a receipt key later must not silently start growing a list payload.
# Counts on the list, receipts on the detail, where the user has already
# chosen the person.
RECEIPT_KEYS = ("patch_ids_by_mode", "patch_ids_chased_without_advance")

# What counts as an item ADVANCING between one meeting and the next.
# Deliberately narrow, and this is the load bearing choice in the chase
# metric: an item advanced only if it CLOSED. A fresh restatement is not
# an advance and a moved due date is not an advance, because motion that
# reads as progress at every checkpoint is the exact illusion this whole
# module exists to break. Published on the wire next to the counts so
# nobody has to guess which definition produced them.
ADVANCE_DEFINITION = "closed_by_the_next_meeting_with_this_person"

# What was actually OBSERVED to make an occasion a chase, published on
# the wire for the same reason ADVANCE_DEFINITION is: a number whose
# definition lives in a docstring is a number nobody can safely reuse.
#
# It matters more here than it looks, because the word "chase" is doing
# inference the evidence does not quite carry. CQ stores no link from a
# QUESTION to an ITEM: the join is meeting level (this item was restated
# in meeting X, and in meeting X the user asked this person N questions),
# so an occasion where the item came up and the user asked about
# something else entirely counts here. That is the same defect shape as
# the mode formerly called `silently_dropped`, in a milder form, and the
# honest mitigation until a question-to-item link exists is to say
# exactly what was seen rather than what it probably means.
CHASE_DEFINITION = "item_raised_in_a_meeting_where_the_user_asked_this_person_a_question"


def _as_date(value: Any) -> Optional[date]:
    """A date from a date, a datetime or an ISO string, else None.

    Same strictness as `follow_through._as_date`: a stored value that is
    not a real day is not guessed at, because a guessed day would put a
    number into a count no stored date supports.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def _as_list(value: Any) -> List[dict]:
    """A JSONB array as a list of dicts, however asyncpg handed it over.

    jsonb arrives as a string from some fetch paths and as a list from
    others, and a caller that got it wrong should degrade to "no
    history", never to an exception on a read route.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return []
    if isinstance(value, list):
        return [e for e in value if isinstance(e, dict)]
    return []


def _as_int(value: Any) -> Optional[int]:
    """A non negative int from an int or a numeric string, else None.

    `value.restatement_count` crosses the wire as text from a `->>`
    select and as an int from a jsonb one, and a stored value that is
    neither is treated as absent rather than raising on a read route.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _norm(name: Any) -> str:
    return (name or "").strip().lower() if isinstance(name, str) else ""


def _history_count(value: Any) -> int:
    """Length of a deadline_history, whether it arrived as the array or
    as a count (some queries select `jsonb_array_length` directly)."""
    counted = _as_int(value)
    if counted is not None:
        return counted
    return len(_as_list(value))


def object_regression(item: Mapping[str, Any]) -> Optional[bool]:
    """Did the OBJECT get vaguer across restatements? Not answerable here.

    This is the hardest signal in the design and the most valuable: "a
    name" becoming "a person to identify" becoming "a shortlist that
    still needs cleaning up" is regression, while "the Renata deck"
    becoming "Renata's slides" is the same object in different words. No
    string comparison separates those. Length does not (the vaguer
    restatement is often longer), lexical overlap does not (both
    restatements share the topic words), and a hedging word list would
    fire on "roughly a shortlist" and miss every case above.

    So this returns None, which in this codebase means CANNOT TELL, and
    the seam is left clean rather than filled with a guess: a cold path
    judge (one batched LLM call, the `semantic_dedup` shape) can be
    handed the ordered restatement texts and its verdicts injected
    through `classify_items(regressions=...)` without any served field
    changing shape. Until that exists the field is honestly null, and no
    count anywhere is derived from it.
    """
    return None


def _restatement_receipts(row: Mapping[str, Any]) -> List[dict]:
    """The stored restatement array, oldest first, normalized.

    Written by the worker's dedup re-observation path: each entry is one
    later meeting saying the same item again, carrying what was said,
    who was named as owner and what date was attached, plus the origin
    so a client can tap through to the meeting. Empty for every item
    that predates that write path, which today is all of them.
    """
    out = []
    for e in _as_list(row.get("restatements")):
        out.append({
            "observed_at": e.get("observed_at"),
            "text": e.get("text"),
            "owner": e.get("owner"),
            "deadline": e.get("deadline"),
            "deadline_date": e.get("deadline_date"),
            "origin_id": e.get("origin_id"),
        })
    return out


def _hop_count(row: Mapping[str, Any], restatements: Sequence[dict]) -> int:
    """How many times this one object has come back.

    `restatement_count` is the monotonic counter the write path keeps
    precisely because the receipts array is capped: an item restated
    fourteen times keeps ten receipts and the count still reads
    fourteen. The deadline_history length is folded in with a max rather
    than added, because on this write path a re-date IS a restatement
    and adding them would double count it. For rows that predate the
    restatement array (every production row today) the history length is
    the only hop evidence there is, and a max keeps it honest as a lower
    bound rather than inventing hops from nothing.
    """
    counted = _as_int(row.get("restatement_count")) or 0
    return max(counted, len(restatements), _history_count(row.get("deadline_history")))


def _owner_change(
    row: Mapping[str, Any],
    restatements: Sequence[dict],
    user_label: Optional[str],
) -> Optional[dict]:
    """The last restatement that named a DIFFERENT owner, or None.

    The stored `value.owner` is never rewritten by a re-observation (the
    write path records history, it does not edit the fact), which is what
    makes this computable at all: the patch keeps the owner it was
    created with, and the restatements carry everyone it was handed to
    afterwards. It is also why an absorbed item stays on the original
    owner's ledger, which is correct: the question the user is asking is
    what happened to the thing THEY were owed.

    An entry with no owner is skipped rather than read as a handover.
    `is_self_owned` treats an empty owner as the user's own (right for
    the you_owe ledger, wrong here), and a blank field on a restatement
    means the extractor named nobody, not that the user took it on.
    """
    stated = _norm(row.get("owner"))
    change = None
    for e in restatements:
        owner = e.get("owner")
        if not isinstance(owner, str) or not owner.strip():
            continue
        if _norm(owner) == stated:
            continue
        change = {
            "from": row.get("owner"),
            "to": owner,
            "observed_at": e.get("observed_at"),
            "origin_id": e.get("origin_id"),
            "to_user": is_self_owned(owner, user_label),
        }
    return change


def _meeting_days(appearances: Iterable[Mapping[str, Any]]) -> List[date]:
    """Distinct days this person was actually PRESENT, oldest first.

    Presence grade, the same predicate the 17a signals use: someone
    merely named in a room did not attend it, and counting a mention as
    a meeting would let an item look abandoned because the person's name
    came up twice in rooms they were never in.
    """
    days = set()
    for a in appearances or ():
        if not is_presence_grade(a):
            continue
        d = _as_date(a.get("last_seen_at"))
        if d:
            days.add(d)
    return sorted(days)


def _chase_meetings(appearances: Iterable[Mapping[str, Any]]) -> Dict[str, dict]:
    """origin_id -> {day, asked_by_user} for this person's meetings.

    `asked_by_user` is None when the meeting carried no question metric
    at all, which is every meeting ingested before migration 37 and can
    never be backfilled. None here means CANNOT TELL whether the user
    pressed, and it is counted as its own number rather than folded into
    either answer.
    """
    out: Dict[str, dict] = {}
    for a in appearances or ():
        origin = a.get("origin_id")
        if not origin or not is_presence_grade(a):
            continue
        day = _as_date(a.get("last_seen_at"))
        explicit = a.get("questions_from_user_explicit")
        inferred = a.get("questions_from_user_inferred")
        asked = None
        if isinstance(explicit, int) or isinstance(inferred, int):
            asked = (explicit or 0) + (inferred or 0)
        out[str(origin)] = {"day": day, "asked_by_user": asked}
    return out


def _chases(
    row: Mapping[str, Any],
    restatements: Sequence[dict],
    meeting_days: Sequence[date],
    chase_meetings: Mapping[str, dict],
    completed_at: Optional[date],
) -> Dict[str, Any]:
    """How often this item was chased, and how often the chase moved it.

    The metric this replaced was questions RECEIVED, and it was wrong.
    Measured by hand against the transcripts, the volume is nearly level
    (twelve questions to one person, ten to another) while the two sets
    are not the same act: one set is chases on items already in the
    ledger that produced no advance, three of them on one item across
    three meetings, and the other is substantive probing of somebody
    already ahead of the user. A card built on volume would have
    asserted something the data contradicts. So the count is chases that
    produced no advance, and questions received stays a separate number
    because the two answer different questions.

    Both halves of the join are already stored. A restatement records
    that THIS item came up in a specific meeting (`origin_id`), and
    `person_appearances` records, for the same meeting, how many
    questions the user asked THIS person. An occasion where both are
    true is a chase.

    Known boundary, published as CHASE_DEFINITION rather than left in
    this docstring: that join is MEETING level, not question level. CQ
    stores no link from a question to an item, so an occasion where the
    item came up and the user asked about something else counts. The
    word "chase" is therefore doing a little inference the evidence does
    not carry, which is the same defect that renamed `silently_dropped`,
    milder. Naming the observation on the wire is the mitigation until a
    question-to-item link exists; a client that only trusts what was
    seen can read the definition and decide.

    Three outcomes, and the third is why this cannot be one number:

    - resolved and no advance: there was a later meeting with this
      person and the item had not closed by it.
    - resolved and advanced: it closed by that next meeting. See
      ADVANCE_DEFINITION for how narrow that is, deliberately.
    - unresolved: the chase happened in the most recent meeting, so
      nothing has had a chance to happen yet. Counting it as "no
      advance" would manufacture the finding out of recency.

    `unmeasurable` is the fourth number and the honest one: the item
    came up in a meeting whose question metric does not exist. On the
    day this ships that is every meeting there has ever been.
    """
    days = sorted(meeting_days)
    occasions: List[dict] = []
    without_advance = advanced = unresolved = unmeasurable = 0

    for e in restatements:
        origin = e.get("origin_id")
        meeting = chase_meetings.get(str(origin)) if origin else None
        if meeting is None:
            # Restated in a room this person was not in. The user did not
            # chase THEM, whatever else happened to the item.
            continue
        asked = meeting["asked_by_user"]
        if asked is None:
            unmeasurable += 1
            continue
        if asked <= 0:
            # The item came up and the user asked this person nothing.
            # Not a chase, and not evidence of anything else either.
            continue
        on = _as_date(e.get("observed_at")) or meeting["day"]
        next_day = next((d for d in days if on and d > on), None)
        if next_day is None:
            outcome, unresolved = None, unresolved + 1
        elif completed_at is not None and completed_at <= next_day:
            outcome, advanced = True, advanced + 1
        else:
            outcome, without_advance = False, without_advance + 1
        occasions.append({
            "origin_id": str(origin),
            "on": on.isoformat() if on else None,
            "next_meeting_on": next_day.isoformat() if next_day else None,
            # True, False, or null for "no later meeting yet".
            "advanced": outcome,
        })

    return {
        # Occasions where the item came up AND the user asked this
        # person at least one question in that meeting.
        "total": len(occasions),
        "without_advance": without_advance,
        "with_advance": advanced,
        "unresolved": unresolved,
        # Came up, but that meeting predates the question metric, so
        # whether it was a chase is unknowable. Never a zero in disguise.
        "unmeasurable": unmeasurable,
        # Both definitions travel with the counts. The second one is the
        # honest boundary of the first: the join is meeting level, not
        # question level, so `total` counts occasions where the item came
        # up and the user asked SOMETHING, not occasions CQ saw the user
        # ask about this item.
        "chase_definition": CHASE_DEFINITION,
        "advance_definition": ADVANCE_DEFINITION,
        "occasions": occasions[:RESTATEMENT_RECEIPT_CAP],
    }


def classify_item(
    row: Mapping[str, Any],
    today: date,
    meeting_days: Sequence[date] = (),
    user_label: Optional[str] = None,
    min_meetings_not_raised: int = MIN_MEETINGS_NOT_RAISED,
    regression: Optional[bool] = None,
    chase_meetings: Optional[Mapping[str, dict]] = None,
) -> Dict[str, Any]:
    """One item's closure mode, with the receipts behind it.

    `meeting_days` are the days the OWNER of this item was present with
    the user, oldest first, so "fell out of the conversation" is counted
    in meetings rather than in elapsed days.
    """
    restatements = _restatement_receipts(row)
    hops = _hop_count(row, restatements)
    moves = _history_count(row.get("deadline_history"))
    completed_at = _as_date(row.get("completed_at"))
    first_stated = _as_date(row.get("created_at"))
    due = _as_date(row.get("deadline_date"))

    # When this object was last SAID, which is the anchor the drop test
    # runs on. The newest restatement if there is one, else the day the
    # item was first stored. Deliberately not `updated_at`: an admin
    # edit, a vouch or a shelve moves that, and none of them is anybody
    # saying the item out loud.
    stated_days = [d for d in (_as_date(e.get("observed_at")) for e in restatements) if d]
    last_stated = max(stated_days) if stated_days else first_stated

    meetings_since = (
        sum(1 for d in meeting_days if d > last_stated) if last_stated else None
    )

    change = _owner_change(row, restatements, user_label)
    is_open = completed_at is None
    shelved = bool(row.get("shelved_at"))

    modes: List[str] = []
    if completed_at is not None:
        modes.append(DELIVERED)
    if change is not None:
        modes.append(ABSORBED_BY_USER if change["to_user"] else REASSIGNED)
    # An OPEN item's mode only, and it needs three things at once: a date
    # that has passed, an owner the user has met since, and nobody
    # raising it in those meetings. It says the item has not come up; it
    # does NOT say nothing happened to it, because a meeting cannot see
    # an email. Shelved items are excluded on the ledger's usual
    # principle: "Let it go" is the user releasing the item, so the
    # silence afterwards is the user's own decision.
    if (
        is_open and not shelved
        and due is not None and due < today
        and meetings_since is not None
        and meetings_since >= min_meetings_not_raised
    ):
        modes.append(NOT_RAISED_SINCE)
    if is_open and moves > 0:
        modes.append(RE_DATED)
    # The molt: said again, and again, with the date never moving. A
    # re-dated item is at least being managed against a calendar.
    if is_open and moves == 0 and hops > 0:
        modes.append(RESTATED)
    if not modes:
        modes.append(OPEN)
    modes.sort(key=MODE_PRECEDENCE.index)

    # Days from the FIRST statement to the close, or to today while it is
    # still open. Null when the item carries no created_at, which is a
    # cannot-tell rather than a zero.
    days_open = None
    if first_stated is not None:
        days_open = ((completed_at or today) - first_stated).days

    return {
        "patch_id": str(row.get("patch_id")) if row.get("patch_id") else None,
        "type": row.get("patch_type"),
        "text": row.get("text"),
        # The RAW extracted surface form, never canonicalized, the same
        # rule the ledger item carries (doc 16 section 8d): it is the
        # only string that will line up against the app's own ledger.
        "owner": row.get("owner"),
        "origin_id": str(row["origin_id"]) if row.get("origin_id") else None,
        "project_id": row.get("project_id"),
        "mode": modes[0],
        # Everything else that is also true of this item, so the headline
        # never hides a second fact. Always a list, possibly length one.
        "modes": modes,
        "hop_count": hops,
        "deadline_moves": moves,
        "deadline": row.get("deadline"),
        "deadline_date": row.get("deadline_date"),
        "overdue_since": row.get("overdue_since"),
        "first_stated_on": first_stated.isoformat() if first_stated else None,
        "last_stated_on": last_stated.isoformat() if last_stated else None,
        "days_open": days_open,
        # Meetings with this person since the item was last said out
        # loud. Null means no first-statement date to count from, never
        # zero meetings.
        "meetings_since_last_statement": meetings_since,
        "owner_change": change,
        # Chases on this item, and how many of them moved it. See
        # _chases: this is the metric that carries the follow up finding,
        # and questions RECEIVED is kept separate rather than folded in
        # because measuring the two as one produced a false claim.
        "chases": _chases(
            row, restatements, meeting_days, chase_meetings or {}, completed_at
        ),
        # Null is CANNOT TELL and is the only value this ships with. See
        # object_regression() for exactly what would be needed to answer
        # it, and why a string heuristic is not it.
        "object_regression": regression if isinstance(regression, bool) else None,
        "completed_at": row["completed_at"].isoformat()
        if isinstance(row.get("completed_at"), (datetime, date))
        else row.get("completed_at"),
        "completion_source": row.get("completion_source"),
        "completion_evidence": row.get("completion_evidence"),
        "shelved_at": row.get("shelved_at"),
        # The receipts. Every count in the summary opens into these.
        "restatements": restatements[:RESTATEMENT_RECEIPT_CAP],
        "deadline_history": _as_list(row.get("deadline_history"))[:RESTATEMENT_RECEIPT_CAP],
    }


def classify_items(
    rows: Iterable[Mapping[str, Any]],
    today: date,
    appearances: Iterable[Mapping[str, Any]] = (),
    user_label: Optional[str] = None,
    min_meetings_not_raised: int = MIN_MEETINGS_NOT_RAISED,
    regressions: Optional[Mapping[str, bool]] = None,
) -> List[Dict[str, Any]]:
    """Every item on one person's ledger, classified, in a total order.

    `regressions` is the seam described in `object_regression`: a
    patch_id keyed map of verdicts from a future cold path judge. Absent
    (always, today) every item reports null for it.
    """
    appearances = list(appearances or ())
    days = _meeting_days(appearances)
    chase_meetings = _chase_meetings(appearances)
    verdicts = regressions or {}
    items = [
        classify_item(
            r, today,
            meeting_days=days,
            user_label=user_label,
            min_meetings_not_raised=min_meetings_not_raised,
            regression=verdicts.get(str(r.get("patch_id"))),
            chase_meetings=chase_meetings,
        )
        for r in rows or ()
    ]
    # A total order, so two identical calls render identically: most hops
    # first (the molt is the headline), then oldest first, then patch id.
    items.sort(key=lambda i: (
        -i["hop_count"],
        i["first_stated_on"] or "9999-12-31",
        i["patch_id"] or "",
    ))
    return items


def summarize(items: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Totals over classified items, with the ids behind every count.

    `items` is the denominator and is always present, because a count
    without one is the thing the client is supposed to be able to refuse
    to render. No ratio is computed here at any denominator.

    `median_hop_count` is null on an empty set, never NaN and never zero:
    GhostPour serializes with allow_nan false, and one non finite float
    turns the whole person payload into a 502. Nothing else in this
    module produces a float at all.
    """
    by_mode = {m: 0 for m in MODE_PRECEDENCE}
    ids: Dict[str, List[str]] = {m: [] for m in MODE_PRECEDENCE}
    hops: List[int] = []
    chased_ids: List[str] = []
    chases = chases_without_advance = chases_unmeasurable = 0
    per_item_without_advance: List[int] = []
    for i in items or ():
        mode = i.get("mode") or OPEN
        by_mode[mode] = by_mode.get(mode, 0) + 1
        ids.setdefault(mode, []).append(i.get("patch_id"))
        h = i.get("hop_count")
        if isinstance(h, int):
            hops.append(h)
        c = i.get("chases") or {}
        chases += c.get("total", 0)
        chases_unmeasurable += c.get("unmeasurable", 0)
        no_advance = c.get("without_advance", 0)
        chases_without_advance += no_advance
        if no_advance:
            chased_ids.append(i.get("patch_id"))
            per_item_without_advance.append(no_advance)
    return {
        "items": len(items or ()),
        # Every mode key is always present, per the additive rule: a
        # client decodes one shape, and a zero here is measured.
        "by_mode": by_mode,
        # The traceability guarantee: "six items, median three hops" is
        # openable into exactly which six.
        "patch_ids_by_mode": ids,
        "median_hop_count": round(median(hops), 1) if hops else None,
        "max_hop_count": max(hops) if hops else None,
        # Pulled out because it is the count the design is FOR, and a
        # client should not have to know the mode vocabulary to find it.
        # It counts items that have not COME UP, which is not the same
        # claim as items nobody worked on: see NOT_RAISED_SINCE. The
        # sentence a client can safely write from it is per item, off
        # `meetings_since_last_statement`.
        "not_raised_since": by_mode.get(NOT_RAISED_SINCE, 0),
        # The peak of that per item number, so "has not come up in your
        # last 4 meetings with her" is available without walking items.
        # Null, never zero, when nothing is in this mode at all.
        "max_meetings_not_raised": (
            max(
                (i.get("meetings_since_last_statement") or 0)
                for i in (items or ())
                if i.get("mode") == NOT_RAISED_SINCE
            )
            if by_mode.get(NOT_RAISED_SINCE, 0) else None
        ),
        # The follow up pressure metric. NOT questions received: that is
        # a separate count on the person, kept separate because the two
        # answer different questions and measuring them as one produced
        # a claim the data contradicts (near level volume, opposite
        # kinds of question). This one is chases on items already in the
        # ledger that moved nothing.
        "chases": chases,
        "chases_without_advance": chases_without_advance,
        "items_chased_without_advance": len(chased_ids),
        # "Three of them on the same item across three meetings" is the
        # sentence the finding is actually made of, so the number behind
        # it is served rather than left to be recomputed.
        "max_chases_without_advance_on_one_item": (
            max(per_item_without_advance) if per_item_without_advance else None
        ),
        # Chases CQ cannot see: the item came up in a meeting with no
        # question metric. On the day this ships this is every meeting,
        # and a client that renders the count above without this one is
        # reporting a floor as a total.
        "chases_unmeasurable": chases_unmeasurable,
        "chase_definition": CHASE_DEFINITION,
        "chase_advance_definition": ADVANCE_DEFINITION,
        "patch_ids_chased_without_advance": chased_ids,
    }


def today_utc() -> date:
    """The UTC day. One place, so every bucket in this module agrees and
    output stays byte stable within a day (the recall cache rule)."""
    return datetime.now(timezone.utc).date()
