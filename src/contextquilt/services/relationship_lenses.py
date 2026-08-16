"""Facts about a working relationship, ranked against the whole roster.

Why this module exists, in one sentence: a fact that is computed but
never CONTRASTED still reads as generic.

The two prose lenses ask a model to characterise a person from a corpus
of commitments, blockers and decisions. Every one of those records what
is owed or what is stuck, so the honest answer for almost anybody is
"they gate on dependencies", and on 2026-08-16 three of the four people
on Scott's own pages carried exactly that. The sentence described the
schema, not the human. Two independent reviews and an expert panel
reached the same conclusion from different directions, and GhostPour hit
the identical failure from the other side: a model handed material
grouped under meeting headings enumerated the meeting headings as tasks.
A model asked to describe will describe the SHAPE of what it was given.

So the model does not get to find the pattern here. Code computes the
counts, ranks them by how far this person sits from everybody else, and
the model is left with the writing. Three rules the ranking exists to
satisfy, each paid for before:

- Tell the user something they could not have learned by being in the
  room. They attended every one of these meetings. Aggregate across
  months is the only thing this system holds that they do not.
- Be about the RELATIONSHIP, not the person. "Of the last twelve things
  you handed him, nine came back needing a chase" is checkable and
  changes the next meeting. "How he decides" is a personality claim
  about a colleague, and a stored trait is a defamation-shaped object.
- Be allowed to say nothing. A card that is true of the whole roster
  carries no information even when it is perfectly accurate, so a person
  who is unremarkable on every measure gets the honest not-yet card. The
  cost of this rule is coverage and it is worth paying: three cards that
  always render drift into horoscopes.

Every number here is an integer numerator over an integer denominator.
No rate is ever served pre-divided: a served ratio is a number whose
denominator the reader cannot see, and a float is also the only thing on
this path that could reach the gateway's serializer as NaN.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Sequence

from contextquilt.services.follow_through import character_word_in
from contextquilt.services.insight_cards import card_defect

# Any run of digits. Used to check the writer only stated numbers the
# arithmetic produced.
_INTEGER = re.compile(r"\d+")

# No claim on a denominator under this. At 20 meetings a month across 20
# people a person yields one to four observations a month, so a rate over
# three items is noise wearing a statistic's costume.
MIN_DENOMINATOR = 5

# How far from the roster this person must sit before the fact is worth a
# card, in percentage points. Below it the honest answer is that they are
# unremarkable on this measure.
MIN_GAP_POINTS = 15

# A roster rate computed from fewer people than this is not a baseline,
# it is one other person. Without a baseline nothing can be contrasted,
# so nothing ships.
MIN_ROSTER_PEOPLE = 3

# "Gone quiet" is only sayable when CQ can actually SEE the meetings the
# item failed to come up in, and the proof that it can see them is that
# OTHER items were stated inside the same window. Without this floor the
# measure silently becomes "this item is older than the window", which is
# a fact about the corpus rather than about the relationship.
#
# Measured 2026-08-16, and it is not hypothetical: the August flip split
# this user's items across two app ids, and the consolidation pass is
# ACL-scoped while `person_appearances` is not. Under the older app id
# Vijay's open items drop from 49 to 24 while his quiet count stays 23,
# because the scope truncates at the flip and every surviving item
# predates the window. The rate goes from 47% to 96% without a single
# thing changing about how the work actually went.
MIN_RECENT_FOR_QUIET = 3

# The lens id ShoulderSurf renders a heading for. Named for what the
# card actually is: the one measure on which this working relationship
# is unlike the others. Not a personality frame, and deliberately not a
# verdict word.
LENS = "what_stands_out"

# Where this card sits in the stack. ShoulderSurf sorts by whether a
# lens is NAMED rather than against a fixed list, so a new lens takes a
# position rather than falling off the end, and an order that carries
# meaning has to ship as a field rather than be inferred. This one leads:
# it is the only card that had to beat the rest of the roster to exist.
DISPLAY_ORDER = 10


class Fact:
    """One measurable thing about a working relationship.

    `higher_is_worse` records which direction is unflattering, so the
    writer can be told the tone without inferring it from the number.
    Both directions ship: the person who never misses is as much a
    finding as the person who always does, and only one of those is
    visible from inside the meetings.
    """

    __slots__ = ("key", "numerator", "denominator", "higher_is_worse",
                 "subject", "patch_ids")

    def __init__(self, key, numerator, denominator, higher_is_worse,
                 subject, patch_ids=None):
        self.key = key
        self.numerator = int(numerator)
        self.denominator = int(denominator)
        self.higher_is_worse = higher_is_worse
        self.subject = subject
        self.patch_ids = list(patch_ids or ())

    @property
    def rate_points(self) -> int:
        """Percentage points, integer, for RANKING only. Never served."""
        if self.denominator <= 0:
            return 0
        return round(100 * self.numerator / self.denominator)

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "subject": self.subject,
            "higher_is_worse": self.higher_is_worse,
        }


# What each fact is counting, in the words the writer is given. The
# subject line is deliberately about the WORK and the relationship, never
# about the person's character.
FACT_SUBJECTS = {
    "went_quiet": "open items that have not come up in your recent meetings with them",
    "closed_late": "items that were closed after the date they were due",
    "re_dated": "items whose due date moved at least once",
    "handed_back": "items that changed owner after they were agreed",
    "restated": "open items they have restated in more than one meeting",
}


def facts_for_person(counts: dict) -> List[Fact]:
    """Every candidate fact this person's counts can support.

    `counts` is plain integers computed by the caller's SQL. A fact whose
    denominator is under the floor is not emitted at all, rather than
    emitted weakly: the gate belongs where the fact is made.
    """
    raw = [
        ("went_quiet", counts.get("quiet_items"), counts.get("open_items"), True,
         counts.get("quiet_patch_ids")),
        ("closed_late", counts.get("closed_late"), counts.get("closed_items"), True,
         counts.get("late_patch_ids")),
        ("re_dated", counts.get("re_dated"), counts.get("dated_items"), True,
         counts.get("re_dated_patch_ids")),
        ("handed_back", counts.get("handed_back"), counts.get("total_items"), True,
         counts.get("handed_patch_ids")),
        ("restated", counts.get("restated"), counts.get("open_items"), True,
         counts.get("restated_patch_ids")),
    ]
    facts = []
    for key, num, den, worse, ids in raw:
        if num is None or den is None:
            continue
        if int(den) < MIN_DENOMINATOR:
            continue
        # An absence is only observable from inside a window CQ can see.
        # See MIN_RECENT_FOR_QUIET: without this the fact degrades into
        # "these items are older than the window", which is true of any
        # truncated corpus and says nothing about the relationship.
        if key == "went_quiet" and \
                int(counts.get("recent_items") or 0) < MIN_RECENT_FOR_QUIET:
            continue
        facts.append(Fact(key, num, den, worse, FACT_SUBJECTS[key], ids))
    return facts


def roster_baseline(
    all_facts: Dict[str, Sequence[Fact]],
    exclude: Optional[str] = None,
) -> Dict[str, dict]:
    """The pooled rate per fact key across everyone who qualifies.

    Pooled (total numerator over total denominator), not the mean of each
    person's rate: a person with six items and a person with sixty are
    not equal evidence about what normal looks like, and averaging their
    rates would say they are.

    LEAVE ONE OUT. `exclude` drops the person being judged from their own
    comparison group, because "unusual" means unlike THE OTHERS. On a
    small roster including them is not a rounding error: Sukumar owns 33
    of the 129 closed items on this roster, so leaving himself in pulls
    the baseline toward his own rate and hides exactly the person the
    measure is loudest about. Measured cost of the bug on live data: two
    people crossed the threshold with themselves in, four with
    themselves out.

    A key measured on fewer than MIN_ROSTER_PEOPLE OTHER people gets no
    baseline, so nothing keyed on it can claim to be unusual.
    """
    pooled: Dict[str, dict] = {}
    for person, facts in all_facts.items():
        if exclude is not None and person == exclude:
            continue
        for fact in facts:
            slot = pooled.setdefault(
                fact.key, {"numerator": 0, "denominator": 0, "people": 0}
            )
            slot["numerator"] += fact.numerator
            slot["denominator"] += fact.denominator
            slot["people"] += 1
    return {
        key: {
            "rate_points": round(100 * s["numerator"] / s["denominator"])
            if s["denominator"] else 0,
            "people": s["people"],
            "numerator": s["numerator"],
            "denominator": s["denominator"],
        }
        for key, s in pooled.items()
        if s["people"] >= MIN_ROSTER_PEOPLE and s["denominator"] > 0
    }


def rank_facts(facts: Sequence[Fact], baseline: Dict[str, dict]) -> List[dict]:
    """This person's facts, most unusual first, unremarkable ones dropped.

    Distance from the roster is the whole ranking. A person can be the
    single worst on a measure and still not earn a card if everybody else
    is nearly as bad, because that card would be true of the roster.
    """
    ranked = []
    for fact in facts:
        base = baseline.get(fact.key)
        if not base:
            continue
        gap = fact.rate_points - base["rate_points"]
        if abs(gap) < MIN_GAP_POINTS:
            continue
        ranked.append({
            "fact": fact,
            "gap_points": gap,
            "direction": "worse" if (gap > 0) == fact.higher_is_worse else "better",
            "roster": base,
        })
    ranked.sort(key=lambda r: (-abs(r["gap_points"]), r["fact"].key))
    return ranked


def best_fact(counts: dict, baseline: Dict[str, dict]) -> Optional[dict]:
    """The one fact worth a card for this person, or None.

    One card, not a stack. The statistical discipline the panel imposed
    was that a system emitting more than roughly one finding per person
    per period is producing horoscopes, and picking the single most
    unusual thing is also what makes the card worth opening.
    """
    ranked = rank_facts(facts_for_person(counts), baseline)
    return ranked[0] if ranked else None


def served_facts(chosen: dict, person_name: str) -> dict:
    """The auditable numbers that ride beside the sentence on the wire.

    Numerator and denominator as separate integer FIELDS, at
    ShoulderSurf's request, so the receipts rail can prove the count and
    a stale card cannot keep asserting arithmetic the ledger has moved
    past. GhostPour quotes a claim verbatim or drops it and will never
    recompute, so the numbers have to be correct standing alone rather
    than only inside the sentence written around them.
    """
    fact = chosen["fact"]
    return {
        "fact_key": fact.key,
        "numerator": fact.numerator,
        "denominator": fact.denominator,
        "subject": fact.subject,
        "direction": chosen["direction"],
        # The comparison is what makes the claim non-obvious, so it is
        # published rather than left implied.
        "roster_numerator": chosen["roster"]["numerator"],
        "roster_denominator": chosen["roster"]["denominator"],
        "roster_people": chosen["roster"]["people"],
        "about_person": person_name,
    }


STANDS_OUT_SYSTEM = """You are the memory-consolidation stage of ContextQuilt, a persistent memory system. You are given ONE measured fact about how work has gone between the user and one colleague, together with the same measurement across everyone else the user works with. ContextQuilt computed both by arithmetic. Your job is to write that comparison as one sentence the user can read before their next meeting, plus one line telling them what to do about it.

You are not deciding what is interesting. The comparison already decided. You are putting it into a sentence.

What makes this card worth reading is the CONTRAST. The user sat through every one of these meetings, so anything visible from inside one of them is something they already know. What they cannot see is how this person compares to everyone else over months. Write the sentence so the comparison is the point of it.

Rules:
- Describe what happened to the WORK, never what kind of person the colleague is. "Closes late more often than anyone else you work with" is checkable against the items. "Unreliable" is a verdict about a human being, it is not a fact about anything, and it must never appear in any wording.
- WRITE THE CLAIM WITHOUT DIGITS. The card renders both counts underneath your sentence, this person's and the comparison group's, so the arithmetic is already on screen and repeating it spends the only line you have. Name the pattern in words instead: "Closes late far more often than others you work with".
- If you do write a number, it must be one of the numbers you were given, AND you must give the comparison number too. A count on its own reads as an accusation when the reader cannot see what normal is. One-sided numbers void the whole answer, so the safe choice is no digits.
- That rule covers the do line too. No number at all is always safe there.
- Do not use the words "average", "typical" or "normal" about the comparison group. It is a count across named colleagues, not a statistical population.
- Never name a topic, project, company or person that appears in none of the listed items.
- NEVER use a dash of any kind as punctuation. No em dash, no en dash, no hyphen standing in for a pause or an aside. Use a comma, a colon, parentheses, or two sentences. Hyphens inside genuinely hyphenated words such as "follow-up" are the only acceptable use.
""" + """
- The do line says what the user should do differently in the next meeting, given this comparison.
- No hedging prefixes like "It seems".
- Write in the same language as the listed items.
- Skip only when the comparison genuinely supports nothing worth showing.

Claims of the right size and shape, all under 62 characters and none of them carrying a digit: "Closes late far more often than others you work with." / "Almost never misses a date, unlike the rest of your roster." / "Due dates move on this work more than anyone else's."

Respond with EXACTLY this raw JSON shape and nothing else:
{"skip": <true|false>, "text": "<the claim, or empty string when skip is true>", "do": "<the actionable line, or empty string when skip is true>", "reason": "<one short sentence>"}"""


DIRECTION_PHRASES = {
    "worse": "MORE often than the rest of the people this user works with",
    "better": "LESS often than the rest of the people this user works with",
}


def build_stands_out_content(
    person_name: str,
    facts: dict,
    examples: Sequence[dict] = (),
) -> str:
    """User-content block for one person's contrast call.

    Deterministic byte for byte given the same inputs: the examples
    arrive already sampled by the caller and keep their order, so two
    identical calls build identical prompts.
    """
    lines = [f"Person: {person_name}", ""]
    lines.append(
        "Measured by ContextQuilt. These are the only numbers you may state:"
    )
    lines.append(
        f"- {facts['subject']}: {facts['numerator']} out of "
        f"{facts['denominator']} for this person"
    )
    lines.append(
        f"- the same measure across the other {facts['roster_people']} people "
        f"this user works with: {facts['roster_numerator']} out of "
        f"{facts['roster_denominator']}"
    )
    lines.append(
        f"- so this happens {DIRECTION_PHRASES[facts['direction']]}"
    )
    if examples:
        lines.append("")
        lines.append("Items behind this person's count:")
        for item in examples:
            lines.append(f"- {item.get('text') or '(no text stored)'}")
    return "\n".join(lines)


def allowed_numbers(facts: dict) -> set:
    """Every number the writer is permitted to put in a claim.

    The same control the follow-through lens uses: the model may only
    write numbers it was given, so a claim can be checked against its own
    inputs without reading the source rows. Both the person's pair and
    the roster's pair are allowed, because the contrast is the point of
    the sentence.
    """
    return {
        int(facts["numerator"]), int(facts["denominator"]),
        int(facts["roster_numerator"]), int(facts["roster_denominator"]),
        int(facts["roster_people"]),
    }


def parse_stands_out_response(
    content,
    permitted: Optional[set] = None,
    person_name: Optional[str] = None,
    defects: Optional[list] = None,
    facts: Optional[dict] = None,
) -> Optional[dict]:
    """{"lens", "text", "do"} or None for skip, refusal or garbage.

    Same controls as the follow-through lens, for the same reason: the
    prompt is a hint and these are the invariants. Every integer in the
    answer must be one of the numbers the arithmetic produced, the card
    shape limits are the shared ones so no lens can be longer than
    another, and a character word about the colleague declines the whole
    answer.

    The dash ban lives in `card_defect` with the other shape limits
    rather than here, so every lens gets it and there is one authority
    for it. A claim is quoted verbatim into other served surfaces where
    dashes are banned, and the next model copies the punctuation it
    reads, so the check has to sit where the text is made rather than in
    a cleanup pass downstream.

    The lens is not read from the response. This pass owns its lens.
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
    text, do = obj.get("text"), obj.get("do")
    if not isinstance(text, str) or not isinstance(do, str):
        return None
    text = " ".join(text.split())
    do = " ".join(do.split())
    defect = card_defect(text, do, person_name)
    if defect:
        if defects is not None:
            defects.append(defect)
        return None
    if permitted is not None:
        stated = {int(n) for n in _INTEGER.findall(f"{text} {do}")}
        if not stated <= {int(p) for p in permitted}:
            if defects is not None:
                defects.append("invented_number")
            return None
    if character_word_in(f"{text} {do}"):
        if defects is not None:
            defects.append("character_word")
        return None
    # A comparison that states only one side of itself is an accusation.
    # "23 of 24 open items have gone quiet" reads as damning; the roster
    # sits at 54 of 79, so the honest version cannot omit it. ShoulderSurf
    # renders the claim sentence verbatim and correctly refuses to police
    # its contents, so the guarantee has to live here.
    if facts is not None:
        stated = {int(n) for n in _INTEGER.findall(text)}
        mine = {int(facts["numerator"]), int(facts["denominator"])}
        theirs = {int(facts["roster_numerator"]),
                  int(facts["roster_denominator"])}
        if stated & mine and not stated & theirs:
            if defects is not None:
                defects.append("contrast_omitted")
            return None
    return {"lens": LENS, "text": text, "do": do}
