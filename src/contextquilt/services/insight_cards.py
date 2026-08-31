"""What a 16a insight card can physically hold.

Every lens writes into the SAME card, so the shape constraints belong to
the card, not to whichever prompt happened to be written first. This
module is the one home for them and depends on nothing, so both the
model-chosen pass and the computed one can enforce identical limits.

Measured, not guessed. The first four live insights ran 97, 114, 139 and
177 characters of claim and 94 to 148 of do line, because nothing in any
prompt said otherwise and a model asked for "one plain sentence" writes a
comfortable one. The 16a design asks for at most 62 characters of claim,
because the collapsed capsule is one line; iOS measured the real visible
budget beside the lens chip at 30 to 37 characters on an iPhone. So a
177 character claim does not render as a teaser, it renders as
"Priya gates forward..." which is a fragment.

62 is the ceiling here, for three reasons: it is the design's own number
so the client is already built around it, it is defensible as "a short
sentence" rather than an arbitrary squeeze, and it survives the hardest
brief any lens has (a computed claim that must also state a real count,
which fits: "Lands about half of what he commits to, usually a week
late." is 59). The do line gets 90: enough for one imperative with a
qualifying clause, and 40 percent off the longest one shipped.

Both are HARD limits, enforced in the parse rather than requested in the
prompt, because a served claim the UI cannot render is worse than no
claim. A rejected answer costs one call and no write: the person keeps
their candidacy and the next cycle tries again, which is why declining is
safe here and silently shipping is not.
"""

from __future__ import annotations

import re
from typing import Optional

# The collapsed capsule is one line. See the module docstring.
# RAISED FROM 62 on 2026-08-16, and the history matters because the
# squeeze was a mistake worth not repeating.
#
# 62 was the 16a design's number for a ONE LINE collapsed capsule, and
# it was correct for that. ShoulderSurf then shipped TWO LINE capsules
# with word-boundary truncation and the full claim preserved for
# VoiceOver and for the expanded card, which means the capsule is a
# TEASER rather than the whole claim. The ceiling stayed at 62 anyway,
# so for three days every claim was being compressed to fit a constraint
# that no longer existed.
#
# What 62 cost, measured against the claims written before it (#240,
# 2026-08-13): they ran 97 to 177 characters and every one carried an
# "X rather than Y" contrast clause, which is the part that actually
# characterises somebody. "Escalates and documents systemic issues
# across environments RATHER THAN applying local fixes" tells a reader
# what this person does and what they do instead. Squeezed to 62 the
# same fact becomes "Gates forward movement until dependencies resolve",
# which is true of most competent people in a delivery org.
#
# 180 covers the observed range of the claims that worked, with headroom.
# The capsule truncates; the card does not.
MAX_CLAIM_CHARS = 180
MIN_CLAIM_CHARS = 10
# One imperative read in the seconds before a meeting, not a paragraph.
# Raised with the claim, and for the same reason: the do lines written
# before the ceiling ran 94 to 148 characters. A do line has to name
# something specific to be worth reading, and "Ask what is blocking
# dates from holding" fits in 90 precisely because it names nothing.
MAX_DO_CHARS = 150
MIN_DO_CHARS = 5

# What the PROMPT asks for, deliberately below what the parse ALLOWS.
# Measured against the live model at temperature 0: asked for "at most
# 62 characters" it returned 65, every time, on five identical calls.
# Pinned temperature means that is not a lottery a retry could win, it is
# a person who never gets a card, so the ask has to absorb the overshoot
# rather than the ceiling absorbing the model. Anchor low, enforce at the
# real limit, and the habitual overshoot lands inside it.
TARGET_CLAIM_CHARS = 120
TARGET_DO_CHARS = 110

# Defect codes, so a systematic format failure is visible in the logs
# instead of looking like a model that keeps changing its mind.
CLAIM_LENGTH = "claim_too_long"
DO_LENGTH = "do_too_long"
CLAIM_OPENS_WITH_NAME = "claim_opens_with_name"

_TRIM = ",.:;!?'\"“”‘’()"


def opens_with_name(text: str, person_name: Optional[str]) -> bool:
    """Whether the claim's first word is the person's own name.

    Every real claim shipped so far opened with it ("Sukumar gates
    forward movement..."), and the card renders on that person's page,
    under their name. So the name is the one word the reader already has,
    and it costs six to eight characters of a thirty five character
    budget. Stripping it at render time was the obvious fix and the wrong
    one: editing served words on the client is the pattern this whole
    workstream has been retiring. It gets fixed at the generator.
    """
    if not text or not person_name:
        return False
    words = text.split()
    if not words:
        return False
    first = words[0].strip(_TRIM).lower()
    tokens = {t.strip(_TRIM).lower() for t in person_name.split() if t.strip(_TRIM)}
    return bool(first) and first in tokens


CLAIM_HAS_DASH = "claim_dash_punctuation"

# An em dash, an en dash, or a hyphen with a space on either side. A
# hyphen inside a word ("follow-up", "on-time") is genuine hyphenation
# and stays.
_DASH_PUNCTUATION = re.compile(r"[–—]|(?<=\s)-(?=\s)|(?<=\w)\s+-\s+(?=\w)")


def dash_as_punctuation(text: str) -> bool:
    """True when a dash is doing a comma's job somewhere in the text.

    A served claim is model-written text that other models later READ:
    GhostPour quotes it into composed surfaces, and a model copies the
    punctuation in the material it is given. That is why this is checked
    where the text is made rather than scrubbed downstream. A cleanup
    pass fixes one surface; the copy has already happened by then.
    """
    return bool(_DASH_PUNCTUATION.search(text or ""))


def card_defect(
    text: str, do: str, person_name: Optional[str] = None
) -> Optional[str]:
    """The first reason this pair cannot ship as a card, or None.

    Shared by every lens's parse so no card can be shorter or longer on
    one surface than another, and so a new lens cannot forget a limit.
    """
    if not (MIN_CLAIM_CHARS <= len(text) <= MAX_CLAIM_CHARS):
        return CLAIM_LENGTH
    if not (MIN_DO_CHARS <= len(do) <= MAX_DO_CHARS):
        return DO_LENGTH
    if opens_with_name(text, person_name):
        return CLAIM_OPENS_WITH_NAME
    if dash_as_punctuation(text) or dash_as_punctuation(do):
        return CLAIM_HAS_DASH
    return None


def one_card_per_lens(cards):
    """At most one card per lens, newest first, order otherwise kept.

    A person holds several `person` patches (one per surface form the
    extractor used) and an insight stamps whichever was current when it
    was derived, so widening the read to every form (#249) also
    surfaced every form's card. Production 2026-08-16: Sukumar rendered
    two HOW THEY DECIDE chips and two WHAT MOVES THEM, Vijay two
    HOW THEY DECIDE, because each surface form had earned its own.

    The write path no longer creates them (the profile pass merges
    clusters by canonical entity first), but cards already derived are
    in the database and would render forever, so the read collapses
    them too. Newest wins: it was derived from the record as it stood
    most recently, and after the merge it is the one derived from the
    WHOLE record rather than a fraction of it.

    Cards with no lens are passed through untouched rather than
    collapsed into one bucket: an unknown shape is not evidence of
    duplication, and the client renders unknown lenses neutrally.

    EVIDENCE BEATS RECENCY, and "evidence" means the denominator where
    there is one and the COUNT OF EVIDENCE ROWS where there is not. The
    second half was missing until 2026-08-31 and the clause that used to
    stand here, "when both cards carry counts", was the trap: the model
    lenses never carry counts, so for them this collapse was pure
    recency and could keep a card computed from 2 meetings over one
    computed from 32. Measured on
    production 2026-08-16: this user's items are split across two app
    ids by the August flip, 916 rows under the old gateway identity and
    279 under the new one, and the consolidation pass is ACL-scoped, so
    it runs once per app and each run sees a different slice of the same
    person. Sukumar has 86 items on one side and 31 on the other. Taking
    the newest card would hand the page whichever pass happened to
    finish last, which is a coin flip on how much of the record the
    claim was computed from. The card standing on the larger denominator
    is the one computed from more of the truth, so it wins, and the
    outcome stops depending on scheduling.
    """
    def _evidence(card):
        """How much of the record this card was computed from.

        A PAIR, and the second half is the fix for a real starvation.
        The denominator is a COMPUTED lens's own arithmetic base and is
        null for a lens a model reasoned its way to: it counted nothing,
        so it has no counts. `how_they_decide` and `what_moves_them` are
        exactly that family.

        So for them the first element was 0 on every card, the
        comparison below never fired, and the collapse fell back to
        newest-first with nothing in the function saying so. Measured on
        production 2026-08-31: Suresh holds three `how_they_decide`
        cards derived from 32, 21 and 2 meetings, and the collapse kept
        the 2. ShoulderSurf's client requires three evidence rows before
        it will render a lens, so both of his model lenses dropped and
        the person we hold 140 meetings on rendered a single card while
        Pallavi, with one card per lens, rendered all of them.

        Neither side could see it. CQ served three cards, the client
        rendered one, and the number that decided it lived inside a card
        neither was inspecting.

        The evidence rows are the honest fallback: the route has already
        computed them, one per distinct meeting with a LIVE source
        patch, which is the same question the denominator answers for
        the lenses that have one. Compared as a tuple so a real
        denominator still wins outright where one exists.
        """
        facts = (card or {}).get("facts") or {}
        try:
            denominator = int(facts.get("denominator") or 0)
        except (TypeError, ValueError):
            denominator = 0
        rows = (card or {}).get("evidence")
        return (denominator, len(rows) if isinstance(rows, (list, tuple)) else 0)

    best: dict = {}
    order: list = []
    passthrough = []
    # `cards` arrives newest-first (the query orders by created_at DESC),
    # which is the tiebreak when neither card carries counts.
    for card in cards or ():
        lens = (card or {}).get("lens")
        if not lens:
            passthrough.append(card)
            continue
        if lens not in best:
            best[lens] = card
            order.append(lens)
        elif _evidence(card) > _evidence(best[lens]):
            best[lens] = card
    kept = [best[lens] for lens in order]
    return kept + passthrough if passthrough else kept


# The shared half of every card prompt. One text, so the two lens
# families cannot drift on what a card is allowed to look like.
CARD_SHAPE_RULES = f"""- The claim is ONE sentence, and it should be SPECIFIC rather than short. Aim for about {TARGET_CLAIM_CHARS} characters; the hard limit is {MAX_CLAIM_CHARS} and a claim over it is thrown away rather than trimmed. You have room. Use it on detail that distinguishes this person, not on adjectives.
- SAY WHAT THEY DO INSTEAD. The claims that actually characterise somebody carry a contrast: "escalates and documents systemic issues across environments RATHER THAN applying local fixes" tells a reader what this person does and what they do in place of the obvious alternative. "Gates forward movement until dependencies resolve" names a behaviour that is true of most competent people. The second half of that sentence is where the information is, so write it.
- Name the SPECIFIC thing where the evidence gives you one. A named piece of work, a kind of decision, the particular condition they wait for. A claim a reader can check against something they remember beats a claim they have to take on trust.
- NEVER begin the claim with the person's name. The card appears on that person's own page, under their name, so it is the one word the reader already has.
- The do line is ONE imperative sentence, read in the seconds before a meeting. Aim for about {TARGET_DO_CHARS} characters; the hard limit is {MAX_DO_CHARS}. It should name something specific too: "Ask which of the open items still have a real date" beats "Ask about progress". A claim never ships without one."""
