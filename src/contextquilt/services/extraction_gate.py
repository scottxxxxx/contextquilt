"""Whether a transcript is long enough to be worth an extraction call.

RULED BY SCOTT ON 2026-08-31, AFTER SEEING THE PRICE, AND THE PRICE IS
THE POINT OF THIS FILE. Measured over 30 days of the meeting_summary
lane, a floor at 1200 characters would have:

    skipped                89 calls
    of which produced      nothing, 85 of them (96%)
    SAVED                  $0.84 per 30 days
    COST                   4 real extractions, 5 stored patches

For scale, all extraction spend over the same 30 days was $7.93, and
every zero-yield call at any length came to $1.88. So this gate is not
a cost measure in any meaningful sense, and nobody should later defend
it as one or tighten it expecting savings:

    floor  900   saves $0.76   costs  1 patch
    floor 1200   saves $0.84   costs  5 patches
    floor 2000   saves $0.98   costs 37 patches

The trade gets WORSE the harder you gate: 2000 costs seven times the
memory of 1200 for another fourteen cents. If you are reading this
while considering a higher number, that table is the argument against
it. The recommendation from the measurement was to build nothing; Scott
read the numbers and ruled the other way, which is his call to make and
is recorded here so the next reader knows it was informed rather than
accidental.

A NOTE ON THE STATISTIC, because it is why this file nearly did not
exist. The number that motivated a gate was "47% of extraction calls
produce zero patches", which is true and was the wrong statistic. Zero
yield correlates with short transcripts, short transcripts are the
cheapest calls, so the failure RATE concentrates precisely where the
money is not. A rate answers how often; the decision needed how much.

DELIBERATE ASYMMETRY WITH THE GATEWAY. GhostPour's summary injection
floor is 900 while this is 1200, so in the 900 to 1200 band the gateway
will inject recall into a summary for a meeting CQ declines to extract
from. That is coherent rather than a bug: reading memory and writing
memory are different questions, with different failure modes and
different costs. Both sides carry this note in the same words so the
two corroborate rather than merely coexist.

Configured by `CQ_EXTRACTION_MIN_CHARS`; `0` disables the gate
entirely, so a person who finds it losing memory that mattered can turn
it off without a deploy. Declines are LOGGED with the reason and the
length, because a gate that declines silently has no instrument and
"the gate fired", "the model returned nothing" and "extraction never
ran" would otherwise be one observable (the #350 lesson, applied to a
gate written six hours after learning it).
"""

from __future__ import annotations

import os
from typing import Optional

DEFAULT_MIN_TRANSCRIPT_CHARS = 1200

# The reason string, not a boolean. The caller logs it, and a gate that
# reports only that it declined cannot tell you which condition fired.
TOO_SHORT = "transcript_below_extraction_floor"


def min_transcript_chars() -> int:
    """The floor, from config. 0 disables the gate; a bad value uses the default."""
    raw = os.getenv("CQ_EXTRACTION_MIN_CHARS")
    if raw is None or raw.strip() == "":
        return DEFAULT_MIN_TRANSCRIPT_CHARS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MIN_TRANSCRIPT_CHARS
    return value if value >= 0 else DEFAULT_MIN_TRANSCRIPT_CHARS


def why_not_worth_extracting(
    transcript: Optional[str], floor: Optional[int] = None
) -> Optional[str]:
    """The reason this transcript cannot support an extraction call, or None.

    Returns the REASON rather than a bare no, on the same argument as
    `role_semantics.why_not_worth_a_call`: the caller logs it, and one
    string is the difference between a lane that can be audited and one
    that can only be guessed at.

    An EMPTY transcript is not gated here. It never reaches this path
    with content worth extracting anyway, and letting the existing empty
    handling own that case keeps this gate answerable for exactly one
    question.
    """
    limit = min_transcript_chars() if floor is None else floor
    if limit <= 0:
        return None
    if not transcript:
        return None
    if len(transcript) < limit:
        return TOO_SHORT
    return None


def worth_extracting(transcript: Optional[str], floor: Optional[int] = None) -> bool:
    """The boolean spelling of `why_not_worth_extracting`.

    Derived from the reason rather than re-deriving the conditions, so
    the two can never drift apart. #350 shipped because two copies of
    one gate's conditions had already been written once.
    """
    return why_not_worth_extracting(transcript, floor) is None
