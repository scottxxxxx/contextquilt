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

import json
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
# A fourth outcome, reachable ONLY when the caller says it can run the
# judge. Every existing caller keeps today's behaviour byte for byte.
NEEDS_JUDGE = "needs_judge"


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
    judge_available: bool = False,
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

    # THE LEXICAL THRESHOLD CANNOT DO THIS JOB, and the measurement is
    # unambiguous. Across the six most-described people, 52 consecutive
    # pairs: median similarity 0.11, MAXIMUM 0.33, against a 0.6
    # threshold. Zero of 122 rows across 43 people had ever been
    # confirmed. Every meeting appended, so the series recorded
    # PARAPHRASE DRIFT rather than perception change, and "how they are
    # changing" said someone changed ten times in thirteen days.
    #
    # It is not a number that wants tuning. Dropping the threshold to
    # 0.3 confirms 1 pair in 52, and low enough to catch these would
    # start merging genuinely different people's descriptions.
    # "Meeting facilitator and lead" and "Project lead for AI for Work
    # standup" are the same perception in different words and score
    # 0.03. Nothing lexical closes that gap, because there is no shared
    # vocabulary to measure.
    #
    # So an inconclusive lexical score asks a model, exactly as the
    # dedup path's gray zone does. The caller declares whether it can:
    # a backfill or a test that cannot run a judge gets today's answer
    # unchanged, which keeps every existing caller identical.
    if judge_available:
        return {"action": NEEDS_JUDGE, "reason": "lexically_inconclusive",
                "similarity": score}
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


# ====================================================================
# The judge: is this the same perception in different words
# ====================================================================
#
# CONSERVATIVE TOWARD APPEND, and the direction is the whole design.
# A wrong CONFIRM destroys a real perception change, which is the only
# thing this series exists to record. A wrong APPEND leaves one extra
# row, which is the noise we already have. So "unsure" resolves to
# APPEND, a judge failure resolves to APPEND, and an unparseable answer
# resolves to APPEND. The dedup path takes the same posture for the same
# reason: judge failure inserts rather than losing a memory.

JUDGE_SYSTEM = (
    "You compare two descriptions of the SAME person, written after two "
    "different meetings, and decide whether the second says something "
    "NEW about them or simply says the same thing in different words.\n\n"
    "Answer SAME only when the two describe the same role, capacity or "
    "standing. Different wording, different emphasis, more or fewer "
    "details of the same role are all SAME. A person called 'meeting "
    "facilitator and lead' in one and 'project lead facilitating "
    "standup' in another is SAME: one role, two phrasings.\n\n"
    "Answer CHANGED when the second states a different role, a new "
    "responsibility, a move between teams or projects, or a fact about "
    "them that the first does not contain and that a reader would want "
    "to know had appeared.\n\n"
    "When you are unsure, answer CHANGED. Recording a change that was "
    "only a rewording costs one extra line. Recording a rewording as "
    "the same perception erases a change that really happened, and "
    "nothing later can recover it.\n\n"
    "Return raw JSON only, no prose and no code fence, exactly:\n"
    '{"verdict": "SAME" or "CHANGED", "why": "<one short clause>"}\n'
)


def build_judge_content(held: str, observed: str) -> str:
    """The two descriptions, oldest first, unlabelled by date.

    No dates and no meeting ids on purpose: nothing in this system
    persists a meeting date, and a model shown an ingest clock would
    reason about recency instead of about content.
    """
    return (
        f"Held description:\n{(held or '').strip()}\n\n"
        f"New description:\n{(observed or '').strip()}\n"
    )


def parse_judge_verdict(content: Any) -> Optional[bool]:
    """True = same perception, False = changed, None = unusable.

    None and False both lead to APPEND, and they are kept distinct
    anyway so the log can say whether the model declined or the parse
    did. An empty answer with a reason and an empty answer without are
    different states, which is the rule this codebase keeps paying for.
    """
    obj = content
    if isinstance(obj, str):
        match = re.search(r"\{.*\}", obj, re.DOTALL)
        if not match:
            return None
        try:
            obj = json.loads(match.group())
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict):
        return None
    verdict = obj.get("verdict")
    if not isinstance(verdict, str):
        return None
    verdict = verdict.strip().upper()
    if verdict == "SAME":
        return True
    if verdict == "CHANGED":
        return False
    return None


def resolve_judged(same: Optional[bool], similarity_score=None) -> Dict[str, Any]:
    """Turn a judge verdict into the action the write path performs."""
    if same is True:
        return {"action": CONFIRM, "reason": "judge_same_perception",
                "similarity": similarity_score}
    return {
        "action": APPEND,
        # Three distinguishable causes for the same action, because
        # "the model said changed", "the model was unusable" and "the
        # judge never ran" are different facts about the system.
        "reason": "judge_perception_changed" if same is False
        else "judge_unusable",
        "similarity": similarity_score,
    }
