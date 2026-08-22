"""The "Working with X" screen: what to do about what was measured.

The trajectory lens says what changed. This says what to do, and it is
the most inferentially dangerous surface in the People object, so the
constraints are tighter here than anywhere else rather than looser.

WHAT THE DESIGN ASKED FOR AND WHY THIS IS NARROWER.

The design's move cards carry a named negotiation technique plus a "why
it works" backed by observation counts, and its example is: "He acted on
9 of 11 process changes he proposed himself; 2 of 7 proposed to him."
That sentence needs PROPOSAL AUTHORSHIP. ShoulderSurf checked their side
before anyone built it: `PersonCommitment` carries an owner, not a
proposer, and nothing on either side records who suggested a thing. So
the evidence that would make a technique personal to this colleague does
not exist, on any hop, today.

That leaves a fork, and taking the wrong branch is how this ships a
horoscope. The wrong branch is to keep the sentence shape and attach the
counts we DO have, which produces "a calibrated question works on Suresh,
because 23 of his 46 open items went quiet". The counts are real, the
sentence is a non sequitur, and it reads as evidence because there are
numbers in it.

So the split this module makes, and it is the whole design:

  THE SITUATION IS OBSERVED. Counts, subject, receipts, this person.
  THE TECHNIQUE IS NOT. It is general communication practice, offered
  BECAUSE OF the situation, never claimed to work ON this person.

`basis` carries that distinction ON THE WIRE, per doc 16 5.13, because a
client cannot infer it and a docstring cannot travel. A move renders as
"here is what is happening, here is a way to raise it", never as "this
is what works on him". The second is a personality claim about a
colleague, and a stored trait is a defamation-shaped object.

THE TECHNIQUE IS CHOSEN BY CODE. A model handed five facts and four
techniques will find a reason for any pairing, and it will be fluent
about it. The mapping below is fixed, auditable, and argued once.

WHAT REPLACES THE MOCK'S STAT PAIR. "2m 40s before a recommendation"
against "~40s of attention" cannot be sourced by anybody: CQ holds no
timing at all, and ShoulderSurf holds per-segment timing it cannot
attribute to a person, since a diarized speaker label has no join to an
entity id unless that voice was enrolled. `your_half` uses what IS
observed on both sides of the relationship: turns, and questions. It is
the mirror the design wanted, measured in what exists.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence

from contextquilt.services.follow_through import character_word_in
from contextquilt.services.insight_cards import dash_as_punctuation

# How many moves a screen may carry. Three, from the design, and the
# ceiling matters for the same reason the lens stack allows one card per
# lens: a screen that always has something to say about everybody is
# producing horoscopes. Fewer than three is a normal outcome here.
MAX_MOVES = 3

# A move needs a situation with evidence under it. Same floor as the
# roster lens, and for the same reason: a rate over three items is noise
# wearing a statistic's costume.
MIN_EVIDENCE = 5


class Technique:
    """One general communication practice, and when code offers it.

    `label` is what the card shows. `why_situation` is the ONLY rationale
    permitted, and note what it is about: it explains why this approach
    suits THE SITUATION. It never explains why it suits the person.
    """

    __slots__ = ("key", "label", "why_situation")

    def __init__(self, key, label, why_situation):
        self.key = key
        self.label = label
        self.why_situation = why_situation


TECHNIQUES = {
    "calibrated_question": Technique(
        "calibrated_question", "Calibrated question",
        "A question starting with how or what hands the problem over "
        "rather than the request, so the answer is theirs to own.",
    ),
    "accusation_audit": Technique(
        "accusation_audit", "Accusation audit",
        "Naming the objection before it is voiced takes the sting out of "
        "it and buys the rest of the sentence a hearing.",
    ),
    "labelling": Technique(
        "labelling", "Labelling, then silence",
        "Reflecting the priority you observe, then stopping, gets you a "
        "correction or a confirmation before you argue inside the wrong "
        "frame.",
    ),
    "framing": Technique(
        "framing", "Framing the cost",
        "The same ask reads differently depending on what it appears to "
        "cost, so it is worth denominating in whatever is scarce right "
        "now rather than in detail.",
    ),
}

# WHICH TECHNIQUE FOR WHICH SITUATION. Fixed, so a model cannot pick, and
# argued once here rather than re-derived per person.
#
# went_quiet and restated are both "the item keeps not resolving", and the
# thing the user controls is HOW they raise it, so both get the question
# that hands over the method. closed_late and re_dated are both about a
# date not holding, where the useful move is to make the cost of the date
# visible rather than to ask again. handed_back is the one where the user
# is about to sound like they are complaining, so the objection gets named
# first. A rate that has fallen is the one situation where the honest move
# is to check an assumption rather than to press, hence labelling.
TECHNIQUE_FOR = {
    "went_quiet": "calibrated_question",
    "restated": "calibrated_question",
    "closed_late": "framing",
    "re_dated": "framing",
    "handed_back": "accusation_audit",
    "speaking_turns": "labelling",
    "questions_to_you": "labelling",
}

# `basis`, on the wire. The one field that stops this screen becoming a
# personality assessment.
OBSERVED = "observed"
GENERAL = "general_practice"


class Move:
    """One thing to try, and the measured situation it answers."""

    __slots__ = ("situation_key", "numerator", "denominator", "subject",
                 "meetings", "patch_ids")

    def __init__(self, situation_key, numerator, denominator, subject,
                 meetings=0, patch_ids=()):
        self.situation_key = situation_key
        self.numerator = int(numerator or 0)
        self.denominator = int(denominator or 0)
        self.subject = subject
        self.meetings = int(meetings or 0)
        self.patch_ids = [str(p) for p in (patch_ids or ()) if p]

    @property
    def technique(self) -> Optional[Technique]:
        return TECHNIQUES.get(TECHNIQUE_FOR.get(self.situation_key, ""))

    def qualifies(self) -> bool:
        """Enough behind this to be worth a card at all."""
        return (
            self.denominator >= MIN_EVIDENCE
            and self.numerator > 0
            and self.technique is not None
        )


def rank_moves(candidates: Sequence[Move]) -> List[Move]:
    """The moves worth showing, most evidence first, capped.

    Ranked on DENOMINATOR, not on how bad the situation looks. Ranking on
    severity would put the flimsiest, most alarming thing at the top of a
    screen whose whole job is to be actionable, and the design's own rule
    is that a move ships only where evidence can be attached to it.

    Ties break on the situation key so two identical runs produce the
    same screen in the same order. A screen that reshuffles between
    cycles reads as the system changing its mind.
    """
    good = [m for m in (candidates or ()) if m.qualifies()]
    good.sort(key=lambda m: (-m.denominator, -m.numerator, m.situation_key))
    # One move per TECHNIQUE. Two calibrated questions on one screen is
    # one idea printed twice, and it is what a fixed mapping makes easy
    # to do by accident: went_quiet and restated both map to the same
    # technique and a person can qualify on both.
    seen, kept = set(), []
    for move in good:
        key = move.technique.key
        if key in seen:
            continue
        seen.add(key)
        kept.append(move)
        if len(kept) >= MAX_MOVES:
            break
    return kept


def served_move(move: Move, rank: int, say: str, headline: str,
                context: str) -> dict:
    """One move as it ships, with the observed/general seam explicit."""
    technique = move.technique
    return {
        "rank": rank,
        "context": context,
        "headline": headline,
        "say": say,
        "technique": technique.key,
        "technique_label": technique.label,
        # The rationale is about the SITUATION. There is deliberately no
        # field here that could hold "why this works on this person",
        # because nothing observed could fill it.
        "why": technique.why_situation,
        "basis": GENERAL,
        "situation": {
            "basis": OBSERVED,
            "key": move.situation_key,
            "numerator": move.numerator,
            "denominator": move.denominator,
            "subject": move.subject,
            "meetings": move.meetings,
            "patch_ids": list(move.patch_ids),
        },
    }


# --------------------------------------------------------------------
# your_half
# --------------------------------------------------------------------

def your_half(
    your_turns: Optional[int], their_turns: Optional[int],
    your_questions: Optional[int], their_questions: Optional[int],
    meetings: int,
) -> Optional[dict]:
    """The mirror, in what is actually observed on both sides.

    NULL IS UNKNOWN, NEVER ZERO. `turn_count` is null where the row
    predates the metric or the person was not a speaker, and migration 34
    is explicit that there is no backfill because the transcripts are
    gone. A null rendered as a zero would say somebody sat silent through
    eight meetings, which is a claim about a person built out of a
    missing column. So a pair with either side unknown is not served.

    The question counts are the EXPLICIT columns only. Migration 37 keeps
    explicit and inferred separate and they must never be summed: a
    comma-delimited vocative at a sentence edge and a trailing question
    followed by who spoke next are different observations with different
    error rates, and adding them produces a number that is neither.
    """
    stats = []
    if your_turns is not None and their_turns is not None:
        stats.append({
            "key": "speaking_turns",
            "label": "Turns you took",
            "value": int(your_turns),
            "counterpart_label": "Turns they took",
            "counterpart_value": int(their_turns),
            "meetings": int(meetings),
        })
    if your_questions is not None and their_questions is not None:
        stats.append({
            "key": "questions_explicit",
            # Named for exactly what was counted. "Questions you asked
            # them" would imply every question; this is the ones where
            # they were addressed BY NAME, which is a narrower and
            # checkable thing. Doc 16 5.13: a served name may assert only
            # what was observed.
            "label": "Questions you put to them by name",
            "value": int(your_questions),
            "counterpart_label": "Questions they put to you by name",
            "counterpart_value": int(their_questions),
            "meetings": int(meetings),
        })
    if not stats:
        return None
    return {"basis": OBSERVED, "stats": stats}


# --------------------------------------------------------------------
# The writer
# --------------------------------------------------------------------

MAX_SAY_CHARS = 220
MAX_HEADLINE_CHARS = 90
MAX_CONTEXT_CHARS = 40
TARGET_SAY_CHARS = 150
TARGET_HEADLINE_CHARS = 65

_INTEGER = re.compile(r"\d+")

# A script is a sentence the user will SAY OUT LOUD to a colleague. That
# makes one failure mode worse here than anywhere else in the system: a
# line that quotes a statistic at somebody. "You have gone quiet on 23 of
# your 46 open items" is accurate, checkable, and would end a working
# relationship. The counts are the reason the card exists; they are not
# the words.
SAY_HAS_NUMBER = "script_quotes_a_number"
SAY_TOO_LONG = "script_too_long"
HEADLINE_TOO_LONG = "headline_too_long"
CONTEXT_TOO_LONG = "context_too_long"
CLAIMS_IT_WORKS = "claims_the_technique_works_on_them"

# Phrases that convert a general practice into a claim about this person.
# The `basis` field says GENERAL on the wire; this stops the PROSE saying
# otherwise, because a reader believes the sentence, not the field.
_WORKS_ON_THEM = re.compile(
    r"\b(works? (?:well )?(?:on|with) (?:him|her|them)|"
    r"he responds (?:well )?to|she responds (?:well )?to|"
    r"they respond (?:well )?to|"
    r"he prefers|she prefers|they prefer|"
    r"is more likely to|tends? to respond|will respond)\b",
    re.IGNORECASE,
)


def claims_it_works_on_them(text: str) -> bool:
    """True when general practice has been dressed as personal knowledge."""
    return bool(_WORKS_ON_THEM.search(text or ""))


def move_defect(context: str, headline: str, say: str) -> Optional[str]:
    """The first reason this move cannot ship, or None."""
    if not (1 <= len(context) <= MAX_CONTEXT_CHARS):
        return CONTEXT_TOO_LONG
    if not (10 <= len(headline) <= MAX_HEADLINE_CHARS):
        return HEADLINE_TOO_LONG
    if not (10 <= len(say) <= MAX_SAY_CHARS):
        return SAY_TOO_LONG
    whole = f"{context} {headline} {say}"
    if dash_as_punctuation(whole):
        return "dash_punctuation"
    if character_word_in(whole):
        return "character_word"
    if claims_it_works_on_them(whole):
        return CLAIMS_IT_WORKS
    # The script only. A headline may carry a count; a spoken line may
    # not, and the distinction is the point.
    if _INTEGER.search(say):
        return SAY_HAS_NUMBER
    return None
