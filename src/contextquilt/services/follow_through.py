"""The follow-through lens (16a lens 3): reliability the arithmetic owns.

The first two lenses ask a model to read observations and name a
behavioral pattern. Both of them do land for well evidenced people: one
person on the live corpus carries a `how_they_decide` and a
`what_moves_them` card at once. But many people get nothing from either,
and the declines are not random. The profile call declines on the two
best evidenced people who have no card yet, with the same stated reason
both times and on both the oldest and the newest slice of their record:
the stored observations are task assignments and scheduling notes, so
they describe what a person DOES, and neither prose lens is about that.

This lens is built the other way round, which is why it reaches people
the prose lenses never will. Everything that decides whether someone
follows through is COMPUTED here, from state CQ already owns:
which of their items had a due date that has come due, which closed on
or before it, which closed after it, which are still open past it, and
how often a due date moved. The model never gets a vote on the verdict.
It receives the finished numbers and writes them up in the house voice,
which is also why this lens cannot decline the way the others do:
arithmetic does not decline. When the numbers are too thin to say
anything, the refusal happens HERE, in code, before a call is spent.

What the numbers actually mean, stated once so nothing downstream
overclaims:

- `completed_at` is when CQ LEARNED an item closed (a later meeting, an
  app tap, a chat completion), not when the work landed. Same for
  `overdue_since`, which the deadline sweep stamps the first time it
  finds an item still open past its date.
- So every count here is a fact about the RECORD, not a stopwatch on the
  person. The claim written from it has to read that way, which is what
  the prompt's first rule is for.
- `value.deadline_history` is the only place a MOVED due date survives
  (the dedup path records the displaced pair when a re-observation
  carries a different date). Nothing else in CQ remembers that a date
  changed, so the move count is the one signal here that can never be
  reconstructed later.

Pure functions only: the worker does the I/O, so every threshold and
every verdict in this file is unit-testable without a database.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .insight_cards import CARD_SHAPE_RULES, card_defect

FOLLOW_THROUGH_LENS = "how_they_follow_through"

# The gate, in items and in meetings. Four judged items is the smallest
# set where "usually lands" and "usually slips" are different sentences;
# the meeting spread is the same receipts invariant the other lenses
# carry (a pattern inside one meeting is an anecdote) and is passed in
# from the rule so an app can raise it.
MIN_JUDGED_ITEMS = 4

# How many counted items are quoted into the prompt. The counts are the
# claim; the examples exist so the sentence can be concrete rather than
# generic, and a spread beats a slice (see spread_sample).
MAX_FACT_EXAMPLES = 6

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INTEGER = re.compile(r"\d+")

# The three verdicts. Open vocabulary downstream: a client that does not
# know one should skip the row, never guess it.
ON_TIME = "on_time"
LATE = "late"
OPEN_PAST_DUE = "open_past_due"

# How each verdict is phrased to the model, so the sentence it writes and
# the number it quotes describe the same thing.
OUTCOME_PHRASES = {
    ON_TIME: "closed on or before the due date",
    LATE: "closed after the due date",
    OPEN_PAST_DUE: "still open past the due date",
}

# An English backstop, not the guarantee. The guarantee is that the
# verdict is arithmetic and the prompt forbids character claims; this
# only catches the shape that ships today, in the one language it can.
# A claim in another language passes this list and is still governed by
# the prompt, which is the honest description of what this is worth.
CHARACTER_WORDS = (
    "unreliable", "reliable", "flaky", "lazy", "careless", "sloppy",
    "incompetent", "untrustworthy", "dishonest", "disorganized",
)


def _as_date(value: Any) -> Optional[date]:
    """A date from a date, a datetime or an ISO string, else None.

    Deliberately strict on strings: a `deadline_date` that is not an ISO
    day is a value the sweep's own regex would skip too, and guessing at
    it would put a number in a claim that no stored date supports.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and _ISO_DATE.match(value.strip()):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def judge_item(row: Mapping[str, Any], today: date) -> Optional[str]:
    """One item's delivery verdict, or None when it has none yet.

    None is the common case and the important one: an item with no due
    date cannot be early or late, and an item whose date is still ahead
    has not been asked the question. Neither belongs in a count about
    following through, so neither is counted.

    `late` reads two independent signals and takes either. `overdue_since`
    means the deadline sweep actually FOUND the item open past its date,
    which survives the item closing later. The date comparison catches
    items that closed before any sweep tick saw them. On the live corpus
    the two agree exactly; keeping both means neither the sweep's 6 hour
    cadence nor a cleared stamp can silently turn a slip into an on time.
    """
    due = _as_date(row.get("deadline_date"))
    if due is None:
        return None
    completed = _as_date(row.get("completed_at"))
    if completed is not None:
        if row.get("overdue_since") or completed > due:
            return LATE
        return ON_TIME
    # Open. Shelved is excluded on the same principle the ledger uses:
    # "Let it go" is the user releasing the item, and holding a person to
    # something the user stopped tracking is not a delivery fact. Archived
    # without a completion is excluded too: expiry is not a verdict about
    # anyone, it is CQ forgetting.
    if row.get("shelved_at"):
        return None
    if (row.get("status") or "active") != "active":
        return None
    return OPEN_PAST_DUE if due < today else None


def _moves(row: Mapping[str, Any]) -> int:
    """How many times this item's due date has been superseded."""
    history = row.get("deadline_history")
    if isinstance(history, str):
        try:
            history = json.loads(history)
        except (ValueError, TypeError):
            return 0
    if isinstance(history, list):
        return len(history)
    if isinstance(history, int) and history >= 0:
        return history  # already counted by SQL
    return 0


def judge_items(rows: Iterable[Mapping[str, Any]], today: date) -> Dict[str, Any]:
    """The whole computation, gate-free, so readiness and the pass share it.

    Returns the counted items in a total order (due date, then patch id)
    and the counts over them. Ungated on purpose: the readiness surface
    has to report how close a person is to a threshold they have not met,
    which means it needs the numbers below the threshold too.
    """
    counted: List[Dict[str, Any]] = []
    for row in rows or ():
        outcome = judge_item(row, today)
        if outcome is None:
            continue
        counted.append({
            "patch_id": str(row.get("patch_id")),
            "origin_id": str(row["origin_id"]) if row.get("origin_id") else None,
            "text": row.get("text"),
            "due_on": _as_date(row.get("deadline_date")).isoformat(),
            "outcome": outcome,
            "moves": _moves(row),
        })
    counted.sort(key=lambda i: (i["due_on"], i["patch_id"]))
    moved = [i for i in counted if i["moves"] > 0]
    facts = {
        "judged_items": len(counted),
        # The receipts number. An item with no origin_id is not a receipt
        # a user can tap through to, so it does not raise this count.
        "meetings": len({i["origin_id"] for i in counted if i["origin_id"]}),
        "closed_on_time": sum(1 for i in counted if i["outcome"] == ON_TIME),
        "closed_late": sum(1 for i in counted if i["outcome"] == LATE),
        "open_past_due": sum(1 for i in counted if i["outcome"] == OPEN_PAST_DUE),
        # Published because the model reaches for it: measured live, it
        # writes "9 of 20 due items closed" unprompted, which is true and
        # is a count over these same items. The fix for a number the
        # arithmetic can produce but did not publish is to publish it,
        # not to forbid the sentence.
        "closed_total": sum(
            1 for i in counted if i["outcome"] in (ON_TIME, LATE)
        ),
        "items_with_moved_due_date": len(moved),
        "due_date_moves": sum(i["moves"] for i in moved),
    }
    return {"facts": facts, "items": counted}


def summarize_follow_through(
    rows: Iterable[Mapping[str, Any]],
    today: date,
    min_items: int = MIN_JUDGED_ITEMS,
    min_meetings: int = 3,
) -> Optional[Dict[str, Any]]:
    """The gated form: the facts a card can be written from, or None.

    None is the decline, and it happens before any LLM call exists. Two
    thresholds, both about volume rather than about verdict: a card needs
    enough judged items to be a pattern and enough distinct meetings to
    be receipts. Nothing here declines because the numbers are unflattering
    or mixed; "commits confidently and slips twice before landing" is
    exactly the card this lens is for.
    """
    result = judge_items(rows, today)
    facts = result["facts"]
    if facts["judged_items"] < min_items or facts["meetings"] < min_meetings:
        return None
    return result


def allowed_numbers(facts: Mapping[str, Any]) -> set:
    """Every integer a claim written from these facts may state."""
    return {int(v) for v in facts.values() if isinstance(v, int)}


FOLLOW_THROUGH_SYSTEM = """You are the memory-consolidation stage of ContextQuilt, a persistent memory system. You are given a DELIVERY RECORD for one person: counts that ContextQuilt computed by arithmetic over the items this person owns, and a sample of the items behind those counts. Your job is to write that record up as one sentence a user can read before their next meeting with this person, plus one line telling them what to do about it.

You are not deciding whether this person follows through. The counts already answered that. You are putting the counts into a sentence.

Rules:
- Describe observed delivery behavior, never character. "Slips twice before landing" is a claim a reader can check against the items. "Unreliable" is a verdict about a human being, it is not a fact about anything, and it must never appear in any wording.
- The counts describe what this RECORD shows, so write the claim that way: what has happened to this person's items, not what kind of person they are.
- Never state a number the arithmetic did not produce. Every figure you write must be one of the counts you were given. Digits are welcome, they are short, but an invented one voids the whole answer.
- That rule covers the do line too. "Ask which 3 items are moving" invents a 3 and throws the card away. Write "Ask which of the open items are moving" instead: no number at all is always safe.
- Two numbers in the claim at most. Listing every count spends the whole line on arithmetic the card already shows underneath.
- Never name a topic, project, company or person that appears in none of the listed items.
""" + CARD_SHAPE_RULES + """
- The do line says what the user should do differently in the next meeting, given this record.
- No hedging prefixes like "It seems".
- Write in the same language as the listed items.
- Skip only when the counts genuinely support nothing worth showing.

Claims of the right size and shape, with and without digits: "Lands about half of what he commits to, late." and "11 of 20 dated items are still open."

Respond with EXACTLY this raw JSON shape and nothing else:
{"skip": <true|false>, "text": "<the delivery claim, or empty string when skip is true>", "do": "<the actionable line, or empty string when skip is true>", "reason": "<one short sentence>"}"""


def build_follow_through_content(
    person_name: str,
    facts: Mapping[str, Any],
    examples: Sequence[Mapping[str, Any]],
) -> str:
    """User-content block for one person's follow-through call.

    `examples` is already sampled by the caller (a spread across the
    window, not the oldest slice) and stays in the order it arrives, so
    two identical calls build identical bytes.
    """
    lines = [f"Person: {person_name}", ""]
    lines.append(
        "Delivery record computed by ContextQuilt. These are the only "
        "numbers you may state:"
    )
    lines.append(
        f"- items with a due date that has come due: {facts['judged_items']}, "
        f"spanning {facts['meetings']} different meetings"
    )
    lines.append(f"- closed at all, on time or late: {facts['closed_total']}")
    lines.append(f"- {OUTCOME_PHRASES[ON_TIME]}: {facts['closed_on_time']}")
    lines.append(f"- {OUTCOME_PHRASES[LATE]}: {facts['closed_late']}")
    lines.append(f"- {OUTCOME_PHRASES[OPEN_PAST_DUE]}: {facts['open_past_due']}")
    # Only when there is something to report. A due date that never moved
    # is the normal case, and "0 moves" in the prompt invites a sentence
    # about an absence nobody asked about.
    if facts.get("items_with_moved_due_date"):
        lines.append(
            "- items whose due date was moved to a later date: "
            f"{facts['items_with_moved_due_date']} "
            f"({facts['due_date_moves']} moves in total)"
        )
    lines.append("")
    lines.append("Items behind those counts:")
    for item in examples:
        text = item.get("text") or "(no text stored)"
        lines.append(
            f"- due {item['due_on']}, {OUTCOME_PHRASES[item['outcome']]}: {text}"
        )
    return "\n".join(lines)


def parse_follow_through_response(
    content: Any,
    permitted: Optional[set] = None,
    person_name: Optional[str] = None,
    defects: Optional[List[str]] = None,
) -> Optional[Dict[str, str]]:
    """{"lens", "text", "do"} or None for skip/refusal/garbage.

    The card shape comes from insight_cards, identical to every other
    lens: same ceilings, same ban on opening with the person's own name.
    This lens has the harder brief (a claim that also carries a real
    count inside 62 characters), which is deliberate. If a number will
    not fit, the honest answer is a shorter claim, not a wider card.

    The lens is NOT read from the response: this pass owns its lens, and
    a model that cannot pick the verdict does not get to pick the card
    either. Two checks beyond the shape:

    - Every integer in the claim must be one of the computed counts. This
      is the enforcement half of "never state a number the arithmetic did
      not produce"; the prompt is the hint, this is the invariant.
    - An English character word declines the whole answer. See
      CHARACTER_WORDS for exactly how much that is worth.
    """
    obj = content
    if isinstance(obj, str):
        m = re.search(r"\{.*\}", obj, re.DOTALL)
        if not m:
            return None
        try:
            obj = json.loads(m.group())
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict):
        return None
    if obj.get("skip") is not False and obj.get("skip") is not None:
        return None
    text = obj.get("text")
    do = obj.get("do")
    if not isinstance(text, str) or not isinstance(do, str):
        return None
    text = " ".join(text.split())
    do = " ".join(do.split())
    # Guardrail 5 of the design lives in card_defect: no do line, no
    # card, both or neither, plus the shared size and shape limits.
    defect = card_defect(text, do, person_name)
    if defect:
        if defects is not None:
            defects.append(defect)
        return None
    if permitted is not None:
        stated = {int(n) for n in _INTEGER.findall(text + " " + do)}
        if not stated <= {int(p) for p in permitted}:
            return None
    lowered = f"{text} {do}".lower()
    if any(re.search(rf"\b{w}\b", lowered) for w in CHARACTER_WORDS):
        return None
    return {"lens": FOLLOW_THROUGH_LENS, "text": text, "do": do}
