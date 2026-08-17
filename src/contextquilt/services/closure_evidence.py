"""Is a meeting-detected closure evidence of completion, or of a promise?

WHY THIS EXISTS. The extraction pass emits `resolved_commitments`, and the
worker used to archive every one of them on the spot. Measured on prod
2026-08-17 across all 167 such closures:

    167   auto-closes, 72% of every completion in the database
      0   ever reversed by a human (the uncomplete lane existed the whole
          time, so this is not accuracy, it is invisibility)
     16%  justified by evidence containing NO completion language at all,
          only future intent
     56%  justified by evidence that never names the person who owed it

The qualitative failure is sharper than the counts. Real examples:

    item     Remind Liz about threshold logic before the 11:30 call
    evidence "Suresh commits to remind Liz about threshold logic
              dependency requirements before the 11:30 call"

    item     Integrate endpoint with core; ETA Monday (2026-08-18)
    evidence "Suresh stated 'I will just add ETA for Monday'"

The first closed because the promise was made AGAIN. The second closed
because somebody set a date. Doc 16 5.12 already rules that a restatement
and a re-date are NOT advances, and the item ledger carries `restated`
and `re_dated` modes to say so. The auto-close was doing the opposite of
the rule the rest of the system runs on.

WHY IT MATTERS MORE THAN CLUTTER. A close archives the row, which drops it
out of the ledger population, and on the person page a completed item
moves into `completed_they_owe` (the delivery history). So a wrong
auto-close does not merely lose an obligation, IT CREDITS SOMEONE WITH
WORK THEY DID NOT DO, and it removes the item from the one instrument that
would have shown the error. Completion history is already `completed_at`
gated so that decay can never appear as a delivery; this walks through
that same rule by another door.

WHAT THIS MODULE DOES, AND WHAT IT REFUSES TO DO. It routes a closure into
one of two bands. It does not score, rank, or emit a confidence float: the
evidence either contains the marks of a completed thing or it does not,
and a number would imply a precision the markers cannot carry (doc 16's
rule against synthesized confidence floats applies here too).

    CONFIDENT  close it, tell the app, one tap reopens
    BELIEVED   leave it OPEN, surface it as "looks done, confirm?"

Both bands carry `reasons`, so the wire says WHY an item landed where it
did and the thresholds can be retuned against real data rather than
re-argued from taste.

English markers only, deliberately, and `classify_closure` returns
BELIEVED for text it cannot read. Same call as the behavior sanitizer's
English-only denylist: a marker list that silently passes everything in
another language would route non-English closures to CONFIDENT, which is
the unsafe direction.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

CONFIDENT = "confident"
BELIEVED = "believed"

# Language of a thing that has not happened yet. A commitment to do X is
# the single most common false close, because a restatement and a
# resolution look almost identical to a model reading one line.
FUTURE_MARKERS = (
    "will", "'ll", "commits to", "commit to", "committed to",
    "plans to", "planning to", "going to", "agreed to", "agrees to",
    "intends to", "intend to", "asked to", "asks", "requests",
    "needs to", "should", "to be completed", "to be done", "eta",
)

# Stronger than FUTURE_MARKERS and treated as a veto rather than a soft
# signal, because these say the ITEM ITSELF is still in flight. A plain
# "will" often describes a NEW action after a real completion ("the
# endpoint was deployed; he will monitor it tomorrow"), which should
# still close. "Is working on" cannot mean that.
#
# The receipt: "Srikanth confirmed he is working on the attachment
# download issue and the configuration change is already applied" routed
# CONFIDENT on the strength of "already", closing an item its own
# evidence describes as unfinished.
IN_PROGRESS_MARKERS = (
    "is working on", "are working on", "working on it", "in progress",
    "still working", "in flight", "ongoing", "has started", "started on",
    "is being", "are being", "being shared", "being prepared",
)

# A completion word inside a negation is the opposite of a completion.
# "Vijay did not send it" contains "did" and "send"; without this it
# routes CONFIDENT and closes an item on evidence that it is NOT done.
NEGATION_MARKERS = (
    "did not", "didn't", "has not", "hasn't", "have not", "haven't",
    "was not", "wasn't", "were not", "is not", "isn't", "not yet",
    "never", "will not", "won't", "unable to", "could not", "couldn't",
    "still waiting", "still pending", "blocked on", "no update",
)

# Language of WORK that happened. Deliberately narrow: these are the
# words that make a closure checkable by a human reading one line.
#
# SPEECH ACTS ARE DELIBERATELY ABSENT, and this is the sharpest thing in
# the file. An early draft had "confirmed" in here, and running it over
# the real 167 put these in the auto-close band:
#
#   "Karthik confirmed he WILL PROVIDE the HDK today by end of day"
#   "Vijay confirmed he WILL LEAD the call later today"
#   "Srikanth confirmed he IS WORKING ON the attachment download issue"
#
# All three are promises. "Confirmed", "stated", "said", "mentioned" and
# "confirms" describe somebody SPEAKING, and a sentence can confirm a
# future perfectly well. Matching on them would have laundered the exact
# failure this module exists to catch, through the band that gets no
# human check. Match on what happened to the WORK, never on the fact that
# a person opened their mouth.
#
# "complete" (bare) is deliberately absent while "completed" is present:
# "Pallavi to complete the integration" is an assignment, not a delivery.
PAST_MARKERS = (
    "completed", "is complete", "was complete", "marked complete",
    "done", "did", "sent", "shared", "delivered", "finished",
    "resolved", "provided", "submitted", "deployed", "fixed",
    "already", "went out", "wrapped up", "signed off", "handed over",
)

# Kept as a named list purely so the reasoning above cannot be undone by
# somebody adding one of these to PAST_MARKERS in good faith.
SPEECH_ACT_MARKERS = (
    "confirmed", "confirms", "stated", "states", "said", "says",
    "mentioned", "mentions", "reported", "noted",
)

# An owner first name shorter than this is too collision prone to use as
# a substring test ("Al" matches "also", "always", "alignment").
MIN_OWNER_TOKEN = 3

REASON_NO_EVIDENCE = "no_evidence"
REASON_FUTURE_ONLY = "future_intent_only"
REASON_NO_COMPLETION_LANGUAGE = "no_completion_language"
REASON_OWNER_NOT_NAMED = "owner_not_named_in_evidence"
REASON_NEGATED = "completion_language_is_negated"
REASON_IN_PROGRESS = "described_as_in_progress"
REASON_MIXED_TENSE = "completion_and_future_language_mixed"


def _markers_in(text: Optional[str], markers) -> List[str]:
    """Whole word (or whole phrase) matches only.

    Substring matching was the first cut and it is wrong in both
    directions: "sent " misses a sentence ending in "sent", and "did"
    matches "candidate". Word boundaries fix both, and mean the marker
    lists can stay readable instead of carrying spacing and punctuation
    variants of every entry.
    """
    low = (text or "").lower()
    found = []
    for m in markers:
        pattern = r"(?<!\w)" + re.escape(m.lower()) + r"(?!\w)"
        if re.search(pattern, low):
            found.append(m)
    return found


def owner_first_token(owner: Optional[str]) -> Optional[str]:
    """The owner's first name, lowercased, or None when unusable."""
    if not owner or not isinstance(owner, str):
        return None
    parts = [p for p in re.split(r"[^\w]+", owner.strip()) if p]
    if not parts:
        return None
    first = parts[0].lower()
    return first if len(first) >= MIN_OWNER_TOKEN else None


def owner_named(owner: Optional[str], evidence: Optional[str]) -> Optional[bool]:
    """Does the evidence mention the person who owed this?

    None means the question does not apply: a self owned item carries no
    owner string, and an owner too short to match is a cannot tell rather
    than a no. Never fold either into False, or an unanswerable question
    starts voting.
    """
    token = owner_first_token(owner)
    if token is None:
        return None
    return token in (evidence or "").lower()


def classify_closure(
    owner: Optional[str],
    evidence: Optional[str],
    require_owner_named: bool = True,
) -> Dict[str, Any]:
    """Route one detected closure into CONFIDENT or BELIEVED.

    CONFIDENT requires all of: evidence exists, it contains completion
    language, it is not purely forward looking, and (when the question
    applies) it names the person who owed the thing. Anything else is
    BELIEVED, because the failure direction that matters is crediting a
    delivery that did not happen.
    """
    reasons: List[str] = []
    text = (evidence or "").strip()

    if not text:
        return {
            "band": BELIEVED,
            "reasons": [REASON_NO_EVIDENCE],
            "future_markers": [],
            "past_markers": [],
            "owner_named": None,
        }

    future = _markers_in(text, FUTURE_MARKERS)
    past = _markers_in(text, PAST_MARKERS)
    negations = _markers_in(text, NEGATION_MARKERS)
    in_progress = _markers_in(text, IN_PROGRESS_MARKERS)
    named = owner_named(owner, text)

    if not past:
        # Nothing here says the thing happened. This catches the whole
        # promise-restated family and does not need a future marker.
        reasons.append(REASON_NO_COMPLETION_LANGUAGE)
    if future and not past:
        reasons.append(REASON_FUTURE_ONLY)
    if future and past:
        # BOTH, and no way to tell which clause owns THIS item without
        # parsing. The receipt: "Vijay confirmed he will lead the call
        # later today. Meeting agenda already prepared." closed an item
        # about a call that had not happened, because "already" matched
        # the agenda. Ambiguous means ask.
        reasons.append(REASON_MIXED_TENSE)
    if negations:
        # Completion language inside a negation says the opposite.
        reasons.append(REASON_NEGATED)
    if in_progress:
        # The evidence describes the item as still in flight, whatever
        # else the sentence also contains.
        reasons.append(REASON_IN_PROGRESS)
    if require_owner_named and named is False:
        reasons.append(REASON_OWNER_NOT_NAMED)

    return {
        "band": BELIEVED if reasons else CONFIDENT,
        "reasons": reasons,
        "future_markers": future,
        "past_markers": past,
        "negations": negations,
        "in_progress": in_progress,
        "owner_named": named,
    }
