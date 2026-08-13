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

from typing import Optional

# The collapsed capsule is one line. See the module docstring.
MAX_CLAIM_CHARS = 62
MIN_CLAIM_CHARS = 10
# One imperative read in the seconds before a meeting, not a paragraph.
MAX_DO_CHARS = 90
MIN_DO_CHARS = 5

# What the PROMPT asks for, deliberately below what the parse ALLOWS.
# Measured against the live model at temperature 0: asked for "at most
# 62 characters" it returned 65, every time, on five identical calls.
# Pinned temperature means that is not a lottery a retry could win, it is
# a person who never gets a card, so the ask has to absorb the overshoot
# rather than the ceiling absorbing the model. Anchor low, enforce at the
# real limit, and the habitual overshoot lands inside it.
TARGET_CLAIM_CHARS = 45
TARGET_DO_CHARS = 70

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
    return None


# The shared half of every card prompt. One text, so the two lens
# families cannot drift on what a card is allowed to look like.
CARD_SHAPE_RULES = f"""- The claim is ONE short sentence. AIM for about 7 words and {TARGET_CLAIM_CHARS} characters. The hard limit is {MAX_CLAIM_CHARS} characters: a claim over it is thrown away, not trimmed, so the card is lost entirely. Write the short version first rather than a full sentence you hope will fit. If a detail does not fit, drop the detail.
- NEVER begin the claim with the person's name. The card appears on that person's own page, under their name, so the name is the one word the reader already has and it spends characters you do not have. Write "Gates forward movement until verification is in place", never "Priya gates forward movement until verification is in place".
- The do line is ONE imperative sentence, read in the seconds before a meeting. AIM for about 11 words and {TARGET_DO_CHARS} characters; the hard limit is {MAX_DO_CHARS} and the same rule applies. A claim never ships without one."""
