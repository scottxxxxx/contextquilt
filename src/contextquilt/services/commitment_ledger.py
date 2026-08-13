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
SILENTLY_DROPPED = "silently_dropped"
OPEN = "open"

# Precedence, highest first, and the tie break is deliberate rather than
# alphabetical. An item can satisfy several of these at once (a re-dated
# item that then changed hands, a restated item nobody has raised in
# three meetings), so one of them has to be the headline.
#
# Ownership modes outrank the rest because a change of hands is a
# structural fact about the item that is true forever, while dropped and
# re-dated are statements about the last few weeks. `silently_dropped`
# outranks `re_dated` for the same reason in the other direction: a
# re-date is management, and the whole point of the dropped mode is that
# management STOPPED. Everything an item also qualifies for is served
# alongside in `modes`, so nothing is hidden by the choice.
MODE_PRECEDENCE = (
    DELIVERED,
    ABSORBED_BY_USER,
    REASSIGNED,
    SILENTLY_DROPPED,
    RE_DATED,
    RESTATED,
    OPEN,
)

# Meetings with THAT PERSON, not elapsed days. Two weeks of silence is
# not a drop when the two of them did not meet; two meetings where the
# item never came up is the thing the user cannot see for themselves.
# Two is the smallest number where "it did not come up" is a pattern
# rather than one crowded agenda.
MIN_SILENT_MEETINGS = 2

# How many restatement receipts travel with one item. Matches the write
# path's own cap, so in practice this truncates nothing; it exists so a
# hand-written or backfilled value cannot inflate one item's payload.
RESTATEMENT_RECEIPT_CAP = 10


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


def classify_item(
    row: Mapping[str, Any],
    today: date,
    meeting_days: Sequence[date] = (),
    user_label: Optional[str] = None,
    min_silent_meetings: int = MIN_SILENT_MEETINGS,
    regression: Optional[bool] = None,
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
    # Dropped is an OPEN item's mode only, and it needs three things at
    # once: a date that has passed, an owner the user has since met, and
    # nobody raising it in those meetings. Shelved items are excluded on
    # the same principle the ledger uses everywhere: "Let it go" is the
    # user releasing the item, so its silence afterwards is the user's
    # decision, not a drop.
    if (
        is_open and not shelved
        and due is not None and due < today
        and meetings_since is not None
        and meetings_since >= min_silent_meetings
    ):
        modes.append(SILENTLY_DROPPED)
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
    min_silent_meetings: int = MIN_SILENT_MEETINGS,
    regressions: Optional[Mapping[str, bool]] = None,
) -> List[Dict[str, Any]]:
    """Every item on one person's ledger, classified, in a total order.

    `regressions` is the seam described in `object_regression`: a
    patch_id keyed map of verdicts from a future cold path judge. Absent
    (always, today) every item reports null for it.
    """
    days = _meeting_days(appearances)
    verdicts = regressions or {}
    items = [
        classify_item(
            r, today,
            meeting_days=days,
            user_label=user_label,
            min_silent_meetings=min_silent_meetings,
            regression=verdicts.get(str(r.get("patch_id"))),
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
    for i in items or ():
        mode = i.get("mode") or OPEN
        by_mode[mode] = by_mode.get(mode, 0) + 1
        ids.setdefault(mode, []).append(i.get("patch_id"))
        h = i.get("hop_count")
        if isinstance(h, int):
            hops.append(h)
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
        "silently_dropped": by_mode.get(SILENTLY_DROPPED, 0),
    }


def today_utc() -> date:
    """The UTC day. One place, so every bucket in this module agrees and
    output stays byte stable within a day (the recall cache rule)."""
    return datetime.now(timezone.utc).date()
