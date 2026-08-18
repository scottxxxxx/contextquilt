"""How a person has been DESCRIBED over time, as a series.

Brian's observation, 2026-08-18: Suresh's description read "scrum
master" one day, because he had introduced himself that way to a new
joiner, and something else the next meeting. His ask was not "which is
right" and not "stop the flapping":

    I would want to see every iteration historically of who they think
    this person is. My hats kept changing and that shifted perception.
    That speaks to organizational health and a watchpoint.

The flapping IS the signal. A person wearing different hats across a
project lifecycle is a fact about the project, and it was being thrown
away on every meeting by an overwrite:

    description = COALESCE(NULLIF($1, ''), description)

THE NAME IS `described_as`, NOT `role`. Doc 16 5.10: a served name may
assert only what was observed. We did not observe anybody's role. We
observed how a model, reading one transcript, described them. "Role"
would be a claim about an org chart; "described as" is a claim about our
own record, which is the only one we can support.

WHAT COUNTS AS A NEW ITERATION. Not every observation. A person
described the same way across forty meetings is one perception confirmed
forty times, and forty identical rows would bury the two that mattered.
A paraphrase is not a new perception either, and descriptions are free
LLM prose so byte equality would append almost every meeting. So the
comparison is fuzzy, using the same trigram threshold the patch dedup
runs on, and only a genuinely different description appends.

NOTHING IS EVER REWRITTEN once appended, on the same discipline as
`value.restatements`. The row is a receipt. A later rephrasing that
overwrote it would destroy the thing this exists to keep, which is
exactly the mistake being fixed.

This module is the DECISION only. The write lives in the worker and the
read in the API, so the rule is testable without a database.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional, Sequence

# Same threshold the patch dedup fast path uses for "this is the same
# fact". Above it, two descriptions are the same perception worded
# differently; below it, somebody's understanding actually changed.
SAME_PERCEPTION_SIMILARITY = 0.6

# Nothing shorter is a perception. "VP", "lead" and "" are noise, and a
# two-character description appending as an iteration would make the
# series unreadable.
MIN_DESCRIPTION_CHARS = 12

# How many iterations travel with a person on the read. A series longer
# than this is a story nobody scrolls; the count still says how many
# there were.
MAX_SERIES = 20

APPEND = "append"
CONFIRM = "confirm"
IGNORE = "ignore"


def _norm(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def usable(description: Optional[str]) -> bool:
    """Long enough to be a perception rather than a fragment."""
    return len(_norm(description)) >= MIN_DESCRIPTION_CHARS


def trigrams(text: str) -> set:
    """Postgres pg_trgm's shape: the string padded and cut into threes.

    Reimplemented here so the DECISION is testable without a database,
    and so the worker can make it in one place rather than issuing a
    similarity() query per entity per meeting.
    """
    s = _norm(text)
    if not s:
        return set()
    padded = f"  {s} "
    return {padded[i:i + 3] for i in range(len(padded) - 2)}


def similarity(a: Optional[str], b: Optional[str]) -> float:
    """Jaccard over trigrams, 0.0 to 1.0. Close enough to pg_trgm for a
    threshold decision, and it needs no round trip."""
    ta, tb = trigrams(a or ""), trigrams(b or "")
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def classify_observation(
    description: Optional[str],
    latest: Optional[Mapping[str, Any]],
    origin_id: Optional[str] = None,
    threshold: float = SAME_PERCEPTION_SIMILARITY,
) -> Dict[str, Any]:
    """What this observation does to the series.

    `latest` is the newest existing row (or None). Returns an action and
    why, so the worker logs a reason rather than a silent branch:

      IGNORE   unusable text, or this exact meeting already landed
      CONFIRM  the same perception again: bump the count and the date
      APPEND   a genuinely different description: a new iteration
    """
    if not usable(description):
        return {"action": IGNORE, "reason": "too_short", "similarity": None}

    if latest is None:
        return {"action": APPEND, "reason": "first_observation", "similarity": None}

    # A re-ingest is the same observation arriving twice (doc 19.4). The
    # meeting that last confirmed a perception must not confirm it again
    # and inflate the count into evidence of stability.
    if origin_id and latest.get("last_origin_id") == origin_id:
        return {"action": IGNORE, "reason": "already_observed_in_this_meeting",
                "similarity": None}

    score = similarity(description, latest.get("description"))
    if score >= threshold:
        return {"action": CONFIRM, "reason": "same_perception", "similarity": score}
    return {"action": APPEND, "reason": "perception_changed", "similarity": score}


def series_payload(rows: Sequence[Mapping[str, Any]], cap: int = MAX_SERIES) -> Dict[str, Any]:
    """The wire shape for one person's series, newest first.

    `changed_from` is the whole point: non-null means the perception
    moved, which is the indicator under the name. Null means one stable
    perception and nothing to show.

    Counts BEFORE the cap, same rule as everywhere else, so a long
    history is a number rather than a silent truncation.
    """
    ordered = sorted(
        rows or [],
        key=lambda r: (r.get("first_observed_at") or ""),
        reverse=True,
    )
    if not ordered:
        return {"current": None, "changed_from": None, "iterations": 0,
                "history": [], "truncated": False}

    def one(r):
        return {
            "text": r.get("description"),
            "first_observed_at": r.get("first_observed_at"),
            "last_observed_at": r.get("last_observed_at"),
            # How many meetings said this. Confirmations, never a
            # confidence score: doc 16 forbids a synthesized float, and
            # the count is traceable where a score is not.
            "observation_count": r.get("observation_count") or 1,
            "origin_id": r.get("first_origin_id"),
        }

    return {
        "current": one(ordered[0]),
        "changed_from": one(ordered[1]) if len(ordered) > 1 else None,
        "iterations": len(ordered),
        "history": [one(r) for r in ordered[:cap]],
        "truncated": len(ordered) > cap,
    }
