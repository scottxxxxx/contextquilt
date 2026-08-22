"""How a working relationship is CHANGING, measured against its own past.

The sibling of `relationship_lenses`, and deliberately built from the
same parts. That lens answers "on what measure is this person unlike
everybody else". This one answers "on what measure is this person unlike
THEMSELVES six weeks ago", which is the question a user genuinely cannot
answer from inside the meetings: slow drift is invisible one standup at
a time and obvious over a quarter.

Same division of labour, for the same reason it was imposed there. Code
computes both windows and picks the measure; the model is left with the
writing and may state only the numbers it was handed. The model may
identify, it may not count (doc 19.1).

THREE THINGS THE DESIGN ASKED FOR THAT THIS DELIBERATELY DOES NOT SERVE,
each with its reason, so nobody re-adds them from the mock:

- A PRE-DIVIDED PERCENTAGE ("from 74% to 41%"). Both windows ship as
  integer numerator/denominator pairs and the client divides if it wants
  to show a percent. A served rate is a number whose denominator the
  reader cannot see, and a float is the one thing on this path that can
  reach the gateway's serializer as NaN. Same rule as the roster lens.
- A SLOPE ("about 3% a week"). Two windows support "it was this, it is
  now that". They do not support a per-week rate of change, and nobody
  measured one. A number that sounds derived and is actually invented is
  the exact failure #284 and #306 were both about.
- A DURATION. The mock's "2m 40s" and "~40s" are not sourceable AS A
  PERSON'S NUMBER. Be precise about which half of that is true, because
  the imprecise version was about to become doctrine: CQ holds no timing
  at all (migration 34 gives turn COUNTS and nothing else), while
  ShoulderSurf DOES hold per-segment timing locally and cannot attribute
  it to a person, since a diarized `speakerLabel` has no join to a CQ
  entity_id unless that voice was enrolled. So the honest statement is
  not "nobody has timing", it is "the side with the timing cannot say
  whose it is". What replaces the pair lives in `working_with.your_half`.
- A WEEKLY SERIES, AND ANY TIME AXIS AT ALL. This is the one that nearly
  shipped. CQ NEVER PERSISTS A MEETING DATE. One arrives at ingest
  (`payload.timestamp`, see worker `_process_meeting`), is spent
  resolving relative deadlines, and is dropped on the floor; every
  timestamp that survives is an INGEST clock, including
  `person_appearances.last_seen_at` and the dates on evidence rows. A
  weekly sparkline keyed on those is a chart of when the importer ran,
  and a bulk import would draw a cliff where nothing happened. So the
  windows here are split by MEETING SEQUENCE, never by elapsed time, and
  the series ships as per-meeting buckets carrying `origin_id`. The
  client holds real meeting dates in its own store and is the only side
  that can put these on a time axis, so it does. Found by ShoulderSurf
  reading this design before a line of it shipped, which is rule 1.

NEUTRAL MEASURES STAY NEUTRAL. `closed_late` has an unflattering
direction; speaking more or asking fewer questions does not, and the
compass spec is explicit that neither end of those axes is good or bad.
So valence is a property of the MEASURE, and a neutral measure gets a
direction of "up"/"down" while only a valenced one gets "worse"/"better".
Without this the writer reads a fall in questions as a decline, which is
a verdict on a colleague that nothing observed.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Sequence

from contextquilt.services.follow_through import character_word_in
from contextquilt.services.insight_cards import CARD_SHAPE_RULES, card_defect

_INTEGER = re.compile(r"\d+")

# The lens id ShoulderSurf renders the hero card for. Named for the
# question it answers, in the design's own words.
LENS = "how_theyre_changing"

# Above the roster lens: a change is the only thing on the page the user
# could not have seen from inside the meetings, and the design makes it
# the hero. See `relationship_lenses.DISPLAY_ORDER` for why order is a
# served field rather than a client-side list.
DISPLAY_ORDER = 5

# Each window needs its own evidence. A change measured from two items
# against three is not a change, it is two small numbers. Same floor as
# the roster lens uses for one window, applied to BOTH, because the
# weaker window bounds the claim.
MIN_WINDOW_DENOMINATOR = 5

# How far apart the two windows must sit before the difference is worth
# a card, in percentage points. Below it the honest answer is that
# nothing has changed, and saying so is not a shortfall.
MIN_GAP_POINTS = 20

# An unflattering claim needs instances behind it in the RECENT window,
# not merely a rate. `relationship_lenses.MIN_INSTANCES_FOR_WORSE` in its
# own words: one event is an anecdote, and "more often" asserts a
# pattern nobody observed.
MIN_INSTANCES_FOR_WORSE = 2

# A trajectory is a claim about a stretch of a relationship, and the unit
# is MEETINGS, never elapsed time. CQ cannot honestly say how long ago
# anything happened (see the docstring: no meeting date is ever
# persisted), and "eleven weeks" from ingest clocks is a claim about the
# importer. Meetings is also the better unit on its own merits: doc 16
# 5.10 already settled that "gone quiet" counts in meetings with them
# rather than in days, because a month of not meeting somebody is not the
# same claim as three meetings without mention.
MIN_SPAN_MEETINGS = 8

# Each window has to contain real meetings, not one. Below this the card
# is a claim about a single occasion wearing a trend's costume.
MIN_WINDOW_MEETINGS = 3


class Measure:
    """One thing whose change over time is worth a sentence.

    `valence` is the honest part. "unflattering_up" means a rise is the
    bad direction and the writer may say so; "neutral" means neither
    direction is a judgement and the writer must describe the movement
    without grading it.
    """

    __slots__ = ("key", "subject", "phrase", "unit", "valence")

    def __init__(self, key, subject, phrase, unit, valence):
        self.key = key
        self.subject = subject
        self.phrase = phrase
        self.unit = unit
        self.valence = valence


# The precise `subject` labels the counts ON THE CARD, where there is
# room. The short `phrase` is what the writer is given, because a 50
# character subject becomes a 75 character sentence: the roster lens paid
# three deploys to learn that and the note is in FACT_PHRASES.
MEASURES = {
    "closed_late": Measure(
        "closed_late",
        "items they closed after the date those items were due",
        "closes late",
        "items closed with a date on them",
        "unflattering_up",
    ),
    "speaking_turns": Measure(
        "speaking_turns",
        "speaking turns they took across your meetings together",
        "takes fewer or more turns",
        "meetings where turns were counted",
        "neutral",
    ),
    "questions_to_you": Measure(
        "questions_to_you",
        "questions they addressed to you by name",
        "asks you questions",
        "meetings where questions were counted",
        "neutral",
    ),
}

# Which way the arrow points, in the words the writer gets. Split by
# valence so a neutral measure never acquires a verdict on the way to
# the prompt.
DIRECTION_PHRASES = {
    ("unflattering_up", "up"): "MORE often now than it used to",
    ("unflattering_up", "down"): "LESS often now than it used to",
    ("neutral", "up"): "HIGHER now than it used to be",
    ("neutral", "down"): "LOWER now than it used to be",
}


class Window:
    """One half of a comparison: a count, its base, and which meetings.

    `origin_ids` is ordered oldest to newest and is the ONLY handle on
    when this window ran. It is deliberately not a date range: CQ holds
    no meeting dates, and the client that does can join these ids to its
    own meeting store and render a real time axis. Serving an ingest
    timestamp here would look exactly like the answer and be a different
    question, which is the whole reason this field is ids.
    """

    __slots__ = ("numerator", "denominator", "origin_ids")

    def __init__(self, numerator, denominator, origin_ids=()):
        self.numerator = int(numerator or 0)
        self.denominator = int(denominator or 0)
        self.origin_ids = [str(o) for o in (origin_ids or ()) if o]

    @property
    def meetings(self) -> int:
        """Distinct meetings this window covers.

        Derived from the ids rather than passed alongside them, so the
        count and the receipts cannot disagree. SS's L8: "19 observations
        across 14 meetings" printed one number while holding another, and
        a count computed from the very list it is a count of cannot do
        that.
        """
        return len(set(self.origin_ids))

    @property
    def rate_points(self) -> int:
        """Percentage points, integer, for RANKING only. Never served."""
        if self.denominator <= 0:
            return 0
        return round(100 * self.numerator / self.denominator)

    def as_dict(self) -> dict:
        return {
            "numerator": self.numerator,
            "denominator": self.denominator,
            "meetings": self.meetings,
            "origin_ids": list(self.origin_ids),
        }


def _span_meetings(earlier: Window, recent: Window) -> int:
    """Distinct meetings the whole comparison covers.

    Union, not sum: the two windows are built to be disjoint, but a
    caller that overlaps them would otherwise get a span longer than the
    relationship. The gate should fail on a caller's bug rather than be
    fooled by it.
    """
    return len(set(earlier.origin_ids) | set(recent.origin_ids))


def change_for_measure(key: str, earlier: Window, recent: Window) -> Optional[dict]:
    """The change on ONE measure, or None when it does not qualify.

    Every gate lives here rather than at the call site, so a new caller
    cannot forget one and a rule change lands in a single place.
    """
    measure = MEASURES.get(key)
    if measure is None:
        return None
    if earlier.denominator < MIN_WINDOW_DENOMINATOR:
        return None
    if recent.denominator < MIN_WINDOW_DENOMINATOR:
        return None
    if earlier.meetings < MIN_WINDOW_MEETINGS:
        return None
    if recent.meetings < MIN_WINDOW_MEETINGS:
        return None
    span = _span_meetings(earlier, recent)
    if span < MIN_SPAN_MEETINGS:
        return None
    gap = recent.rate_points - earlier.rate_points
    if abs(gap) < MIN_GAP_POINTS:
        return None
    movement = "up" if gap > 0 else "down"
    if measure.valence == "unflattering_up":
        direction = "worse" if movement == "up" else "better"
        # A pattern needs instances. The gate applies to the window the
        # claim is ABOUT, which is the recent one.
        if direction == "worse" and recent.numerator < MIN_INSTANCES_FOR_WORSE:
            return None
    else:
        direction = movement
    return {
        "measure_key": key,
        "measure": measure,
        "earlier": earlier,
        "recent": recent,
        "gap_points": gap,
        "movement": movement,
        "direction": direction,
        "span_meetings": span,
    }


def best_change(windows: Dict[str, tuple]) -> Optional[dict]:
    """The one change worth a card for this person, or None.

    `windows` maps a measure key to (earlier, recent). One card, not a
    stack: the same discipline the roster lens states, that a system
    emitting more than about one finding per person per period is
    producing horoscopes.

    Ties break on the measure key so two identical runs pick the same
    card. A card that changes identity between cycles reads to the user
    as the system changing its mind.
    """
    candidates = []
    for key, pair in (windows or {}).items():
        try:
            earlier, recent = pair
        except (TypeError, ValueError):
            continue
        found = change_for_measure(key, earlier, recent)
        if found:
            candidates.append(found)
    if not candidates:
        return None
    candidates.sort(key=lambda c: (-abs(c["gap_points"]), c["measure_key"]))
    return candidates[0]


def meeting_series(buckets) -> List[dict]:
    """The sparkline's data, as ordered per-MEETING integer pairs.

    Two decisions here, both forced and both worth stating.

    ORIGIN IDS, NOT DATES. CQ never persists a meeting date, so a weekly
    series computed here would be keyed on ingest clocks and would draw a
    cliff wherever a bulk import happened. Each bucket carries the
    `origin_id` instead; the client joins that to its own meeting store,
    which is the only place a real meeting date exists, and lays the
    points out on a real axis. `sequence` is CQ's own ordering, oldest
    first, so a client with no date for some meeting can still draw the
    series in the right order.

    PAIRS, NOT POINTS. A series of pre-divided floats is a chart whose
    denominators the reader cannot see, and a meeting with a denominator
    of zero would divide by it. It also matters at the gateway: GP's
    proxy walks the payload replacing non-finite floats with null (their
    #674, because one bad float 502s the whole person page), so a float
    series has a silent null-shaped failure mode that an integer series
    simply does not have. Integers are never visited by that walk.

    Meetings with no evidence are KEPT with a zero denominator, so the
    client can draw an honest gap instead of a straight line through one.

    Order is meaning. A re-sorted series is a different claim, silently.
    """
    out = []
    for index, bucket in enumerate(buckets or ()):
        origin = (bucket or {}).get("origin_id")
        if not origin:
            continue
        try:
            num = int((bucket or {}).get("numerator") or 0)
            den = int((bucket or {}).get("denominator") or 0)
        except (TypeError, ValueError):
            continue
        out.append({
            "origin_id": str(origin),
            "sequence": index,
            "numerator": num,
            "denominator": den,
        })
    return out


def served_trajectory(
    chosen: dict, person_name: str, series=(), supersedes=(),
) -> dict:
    """The auditable numbers that ride beside the sentence on the wire.

    Everything the card renders and everything a reader would need to
    check it, as separate integer fields. GhostPour quotes a claim
    verbatim or drops it and will never recompute, so each number has to
    be correct standing alone rather than only inside the sentence
    written around it.
    """
    measure = chosen["measure"]
    return {
        "measure_key": chosen["measure_key"],
        "subject": measure.subject,
        "unit": measure.unit,
        "valence": measure.valence,
        "direction": chosen["direction"],
        "movement": chosen["movement"],
        "span_meetings": chosen["span_meetings"],
        "earlier": chosen["earlier"].as_dict(),
        "recent": chosen["recent"].as_dict(),
        "series": meeting_series(series),
        # Lens ids this card REPLACES on this person, so the same
        # arithmetic never renders twice on one screen. ShoulderSurf
        # asked who owns that decision and the answer is CQ: it is an
        # ordering question, ordering authority is already served
        # (`display_order`), and a client inferring it would be a second
        # place the rule lives. Empty list means nothing is superseded,
        # which is not the same as absent.
        "supersedes": [str(x) for x in (supersedes or ())],
        "about_person": person_name,
    }


def allowed_numbers(facts: dict) -> set:
    """Every number the writer is permitted to put in a claim.

    Both windows' pairs, their meeting counts and the span in meetings. A
    claim is often clearer as "across your last 16 meetings" than as a
    bare pair, and every one of these is a number the arithmetic
    produced. Note what is NOT in here: any number of weeks, days or
    months. There is no such number on this path, so a writer that
    states one has invented it and the parse throws the answer away.
    """
    earlier = facts.get("earlier") or {}
    recent = facts.get("recent") or {}
    out = set()
    for block in (earlier, recent):
        for key in ("numerator", "denominator", "meetings"):
            try:
                out.add(int(block.get(key)))
            except (TypeError, ValueError):
                continue
    try:
        out.add(int(facts.get("span_meetings")))
    except (TypeError, ValueError):
        pass
    return out


TRAJECTORY_SYSTEM = """You are the memory-consolidation stage of ContextQuilt, a persistent memory system. You are given ONE measured thing about how work has gone between the user and one colleague, counted over two stretches of their meetings together: an earlier stretch and a recent one. ContextQuilt computed both by arithmetic. Your job is to write that change as one sentence the user can read before their next meeting, one short paragraph explaining what it means for them, and one line telling them what to do about it.

You are not deciding what is interesting. The measurement already decided. You are putting it into words.

What makes this card worth reading is that the user CANNOT SEE IT. They sat through every one of these meetings. A drift this slow is invisible in any single meeting and large across a stretch of them, and the only thing this system holds that they do not is the aggregate. Write so that the change is the point of it.

Rules:
- Describe what happened to the WORK, never what kind of person the colleague is. "Closed 9 of his last 11 items after their date, against 3 of 12 before June" is checkable against the items. "Has become unreliable" is a verdict about a human being, it is not a fact about anything, and it must never appear in any wording.
- STATE BOTH PERIODS' NUMBERS in the claim. A single period's count is not a change, and a change stated without its starting point is an accusation the reader cannot check.
- Any number you write must be one you were given. Do not compute new ones. In particular do NOT work out a percentage, a rate of change, or a difference between the two pairs. If you want to express size, say which pair is bigger, not by how much.
- MEASURE TIME IN MEETINGS, NEVER IN WEEKS, DAYS, MONTHS OR SEASONS. This system does not know when any of these meetings happened. It knows their order and how many there were, and nothing else. "Across your last 8 meetings with him" is a fact. "Over the past eleven weeks" and "since June" are inventions, and so is "recently" if you mean elapsed time by it. Do not name a month. Do not say "lately", "this summer", "these days" or "of late".
- Never name a topic, project, company or person that appears in none of the listed items.
- SOME MEASURES HAVE NO GOOD OR BAD DIRECTION. You will be told whether this one does. When you are told the direction is neutral, describe the movement and do not grade it: taking fewer turns or asking fewer questions is a change in how meetings are running, not a fault and not an improvement. Do not imply decline, disengagement, withdrawal, or effort.
- NEVER use a dash of any kind as punctuation. No em dash, no en dash, no hyphen standing in for a pause or an aside. Use a comma, a colon, parentheses, or two sentences. Hyphens inside genuinely hyphenated words such as "follow-up" are the only acceptable use.
""" + CARD_SHAPE_RULES + """
- The narrative is ONE short paragraph, at most 320 characters, expanding the claim into what it means for the user's own next meeting. It is the only place you may say anything about the SHAPE of the change (that it happened gradually, that it is visible only in aggregate). It still may not state a number you were not given, and the ban on weeks and months applies to it in full.
- The do line STARTS WITH A VERB and is one short instruction. Never open it with a preamble such as "In your next meeting," or "Consider". The reader already knows when they will use it.
- No hedging prefixes like "It seems".
- Write in the same language as the listed items.
- Skip when the two stretches genuinely support nothing worth showing.

BUILD THE CLAIM FROM THE SHORT PHRASE YOU WERE GIVEN, not from the long label beside the counts. The label is precise so the card can show what was measured; it is far too long to put in a sentence.

Respond with EXACTLY this raw JSON shape and nothing else:
{"skip": <true|false>, "text": "<the claim, or empty string when skip is true>", "narrative": "<the paragraph, or empty string when skip is true>", "do": "<the actionable line, or empty string when skip is true>", "reason": "<one short sentence>"}"""


# The expanded card's body. Longer than a claim, shorter than a screen.
MAX_NARRATIVE_CHARS = 320
MIN_NARRATIVE_CHARS = 20
NARRATIVE_LENGTH = "narrative_too_long"
WINDOW_OMITTED = "one_window_only"
GRADED_NEUTRAL = "graded_a_neutral_measure"
ELAPSED_TIME = "stated_elapsed_time"

# Any word that turns an ordered list of meetings into a calendar. CQ
# holds no meeting date, so every one of these is invented however
# plausible it reads, and "eleven weeks" was the single most confident
# sentence in the design that nothing could source.
_ELAPSED = re.compile(
    r"\b(week|weeks|month|months|day|days|year|years|quarter|quarters|"
    r"fortnight|summer|winter|spring|autumn|fall|lately|recently|"
    r"january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\b",
    re.IGNORECASE,
)


def states_elapsed_time(text: str) -> bool:
    """True when the writer put a calendar on a claim that has no dates.

    Separate from `invented_number` on purpose: "over the past eleven
    weeks" trips both, but "since the summer" and "lately" carry no digit
    at all and would sail through a numeric check. The unit is the
    defect, not the figure.
    """
    return bool(_ELAPSED.search(text or ""))

# Words that turn a movement into a verdict. Only checked on NEUTRAL
# measures, where nothing observed supports grading either direction.
# `character_word_in` already catches claims about the person; this
# catches claims about the CHANGE, which is a different sentence and
# slips past that check.
_GRADING = re.compile(
    r"\b(disengag\w*|withdraw\w*|checked out|declin\w*|deteriorat\w*|"
    r"slipp\w*|worse|worsen\w*|improv\w*|better|regress\w*|"
    r"less committed|losing interest|tuning out)\b",
    re.IGNORECASE,
)


def grades_a_neutral_measure(text: str) -> bool:
    """True when neutral movement has been written as good or bad news.

    This is the trajectory lens's own version of the character-word ban,
    and it exists because the two bans catch different sentences. "He has
    become disengaged" is a claim about a person and `character_word_in`
    stops it. "His participation has declined" is a claim about a number,
    passes every existing check, and is still a verdict nothing observed:
    what was measured is that a count went down.
    """
    return bool(_GRADING.search(text or ""))


def parse_trajectory_response(
    content,
    permitted: Optional[set] = None,
    person_name: Optional[str] = None,
    defects: Optional[list] = None,
    facts: Optional[dict] = None,
) -> Optional[dict]:
    """{"lens", "text", "narrative", "do"} or None for skip or garbage.

    The prompt is a hint; these are the invariants. Same posture as every
    other lens: a rejected answer costs one call and no write, the person
    keeps their candidacy, and the next cycle tries again. Declining is
    safe here and silently shipping is not.
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
    narrative = obj.get("narrative")
    if not isinstance(text, str) or not isinstance(do, str):
        return None
    if not isinstance(narrative, str):
        return None
    text = " ".join(text.split())
    do = " ".join(do.split())
    narrative = " ".join(narrative.split())
    defect = card_defect(text, do, person_name)
    if defect:
        if defects is not None:
            defects.append(defect)
        return None
    if not (MIN_NARRATIVE_CHARS <= len(narrative) <= MAX_NARRATIVE_CHARS):
        if defects is not None:
            defects.append(NARRATIVE_LENGTH)
        return None
    whole = f"{text} {narrative} {do}"
    if permitted is not None:
        stated = {int(n) for n in _INTEGER.findall(whole)}
        if not stated <= {int(p) for p in permitted}:
            if defects is not None:
                defects.append("invented_number")
            return None
    if character_word_in(whole):
        if defects is not None:
            defects.append("character_word")
        return None
    if states_elapsed_time(whole):
        if defects is not None:
            defects.append(ELAPSED_TIME)
        return None
    if facts is not None:
        if (facts.get("valence") or "neutral") == "neutral" \
                and grades_a_neutral_measure(whole):
            if defects is not None:
                defects.append(GRADED_NEUTRAL)
            return None
        # A change that states only its recent half is not a change. The
        # roster lens has the same guarantee for its own contrast
        # (`contrast_omitted`) and for the same reason: ShoulderSurf
        # renders the claim verbatim and correctly refuses to police it,
        # so the guarantee has to live where the text is made.
        earlier = facts.get("earlier") or {}
        recent = facts.get("recent") or {}
        stated = {int(n) for n in _INTEGER.findall(text)}
        try:
            mine = {int(recent["numerator"]), int(recent["denominator"])}
            theirs = {int(earlier["numerator"]), int(earlier["denominator"])}
        except (KeyError, TypeError, ValueError):
            mine = theirs = set()
        if mine and stated & mine and not stated & theirs:
            if defects is not None:
                defects.append(WINDOW_OMITTED)
            return None
    return {
        "lens": LENS,
        "text": text,
        "narrative": narrative,
        "do": do,
    }


def retry_note(defect: str, attempt_text: str = "") -> Optional[str]:
    """A corrective line for one bounded retry, or None.

    Same reasoning as `relationship_lenses.retry_note`: a pinned
    temperature makes a plain retry pointless because the same prompt
    returns the same answer, but telling the writer exactly what its last
    answer did wrong is a DIFFERENT prompt and a genuinely different
    question. Only the mechanically recoverable defects get one.
    """
    if defect == "claim_too_long":
        return (
            "Your last answer's claim was "
            f"{len(attempt_text or '')} characters, which is over the limit "
            "and was thrown away. Say the same true thing in a shorter "
            "sentence. Cut a qualifier, not the meaning."
        )
    if defect == NARRATIVE_LENGTH:
        return (
            "Your last answer's narrative was over the limit and was "
            f"thrown away. Keep it under {MAX_NARRATIVE_CHARS} characters. "
            "One short paragraph, not two."
        )
    if defect == WINDOW_OMITTED:
        return (
            "Your last answer stated the recent period's numbers without "
            "the earlier period's. A change needs both halves or the "
            "reader cannot see what it changed from. State both pairs."
        )
    if defect == GRADED_NEUTRAL:
        return (
            "Your last answer graded a measure that has no good or bad "
            "direction. What was observed is that a count moved. Describe "
            "the movement without words like declined, improved, slipped "
            "or disengaged."
        )
    if defect == ELAPSED_TIME:
        return (
            "Your last answer described elapsed time. This system does "
            "not know when any of these meetings happened, only their "
            "order and how many there were. Count in meetings instead, "
            "and remove any week, month, season or named month."
        )
    if defect == "invented_number":
        return (
            "Your last answer contained a number you were not given. Do "
            "not compute percentages, rates or differences. Use only the "
            "counts as they were handed to you."
        )
    return None
