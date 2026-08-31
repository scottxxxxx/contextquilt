"""The one line that fits on cloth.

Woven handoff section 6.3: the `fact` is the record and stays verbatim
in detail; the `headline` is what a tile can hold. From the prototype's
own data, the same patch carries both:

    fact:     "Target market of 60-67% small firms (3-5 attorneys) is
               ideal: low customer acquisition friction compared to
               large law firms, and high retention potential at $3M ARR."
    headline: "60-67% small firms is the sweet spot"

The headline is WRITTEN, not truncated. That is the whole reason this
needs a model: a rule can cut a string and cannot rewrite one, and a cut
string on a tile reads as a broken sentence, which is the most visible
thing on that screen.

WHY AT INGEST AND NOT ON THE READ PATH. CQ's read path makes no LLM
call, by design; that is the zero-latency premise the whole system is
built on. A headline generated per request would put a model in front
of a user waiting for a tab to paint. So it is written once, when the
patch is, and stored on `value.headline`.

THE VALIDATOR IS THE POINT OF THIS MODULE, not the prompt. Section 6.3
lists six rules and a model will break several of them cheerfully, so
they are enforced in code and a headline that breaks one is REFUSED
rather than repaired. Refusing costs a tile; repairing would mean
truncating, which is the exact thing 6.3 forbids and the exact thing
that looks broken on screen. `who_they_are` already works this way: its
parse refuses a summary that invents a number rather than accepting it.

Two of the six are checkable rather than merely assertable, and those
are the ones that matter:

  * A NUMBER IN THE HEADLINE MUST APPEAR IN THE FACT. This catches an
    invented figure, which is the failure that would put a made-up
    dollar amount on a user's home screen in a serif font.
  * IF THE FACT CARRIES A CONCRETE FIGURE, THE HEADLINE SHOULD TOO.
    6.3 says keep the concrete noun or number, and a headline that
    drops "$3M" for "the revenue goal" is the vaguer of the two.

No dashes anywhere, in the prompt or the output. A model copies the
punctuation it is shown, and this text is served content.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

MAX_HEADLINE_CHARS = 48

# Refusal reasons, not booleans. A generator that reports only that it
# refused cannot tell you which rule the model keeps breaking, and that
# is the thing you tune the prompt against.
TOO_LONG = "over_48_chars"
ELLIPSIS = "trailing_ellipsis"
TERMINAL_PERIOD = "terminal_period"
INVENTED_NUMBER = "number_not_in_fact"
DROPPED_FIGURE = "concrete_figure_dropped"
IMPERATIVE = "addresses_the_user"
EMPTY = "empty"
DASH = "contains_a_dash"

# Any figure a reader would call concrete: currency, percentage, a
# range, a bare number. Deliberately NOT matching an unspaced hyphen as
# a separator, because "60-67%" is one figure and splitting it is how
# the stitch labels lost their number.
_FIGURE = re.compile(r"\$[\d,.]+[kmb]?|\b\d[\d,.]*\s?%|\b\d[\d,.\-]*\b", re.I)

# "Remember to", "Ask him", "Follow up on". The quilt states; it does
# not nag (6.3). Checked at the start only: a mid-sentence "ask" is a
# noun as often as a verb.
_IMPERATIVE = re.compile(
    r"^\s*(remember|don'?t forget|make sure|ask|check|follow up|reach out|"
    r"consider|note that|you should|you need)\b", re.I)

_DASHES = ("—", "–")


def _figures(text: str) -> set:
    return {m.group(0).strip().rstrip(".,") for m in _FIGURE.finditer(text or "")}


def why_invalid(headline: Optional[str], fact: str) -> Optional[str]:
    """The rule this headline breaks, or None. Section 6.3, in code.

    Order is cheapest first, and the two content rules last because they
    are the only ones that need the fact.
    """
    text = (headline or "").strip()
    if not text:
        return EMPTY
    if len(text) > MAX_HEADLINE_CHARS:
        return TOO_LONG
    if text.endswith("...") or text.endswith("…"):
        return ELLIPSIS
    # A TERMINAL PERIOD IS ONLY WRONG ON A SINGLE SENTENCE, and the
    # prototype is why. Section 6.3 says "no terminal period", but the
    # artifact's own headline is "Zero data retention. No exceptions." --
    # two clipped sentences whose full stop is doing rhetorical work.
    # What 6.3 is actually guarding against is a headline that reads as
    # a SENTENCE that got cut off, and that is a single sentence with a
    # stop on the end. Two of them read as deliberate.
    #
    # Found by running the prototype's own headlines through this
    # validator, where the first version refused one of the four. A
    # validator that refuses the design is wrong about the design.
    if text.endswith(".") and "." not in text[:-1]:
        return TERMINAL_PERIOD
    if any(d in text for d in _DASHES):
        return DASH
    if _IMPERATIVE.match(text):
        return IMPERATIVE

    head_figures = _figures(text)
    fact_figures = _figures(fact or "")

    # An invented number is the failure that would put a made-up dollar
    # amount on someone's home screen, set in serif, looking certain.
    if head_figures - fact_figures:
        return INVENTED_NUMBER

    # And the softer half of the same rule: if the fact had a figure and
    # the headline kept none, the headline is the vaguer of the two,
    # which is what 6.3's "keep the concrete noun or number" is for.
    if fact_figures and not head_figures:
        return DROPPED_FIGURE
    return None


SYSTEM = (
    "You write the one line that fits on a tile.\n\n"
    "You are given facts remembered from a meeting. For each, write a "
    "headline a person would recognise a week later, in their own "
    "register, with the hedging stripped out.\n\n"
    "Rules, all of them hard:\n"
    "1. At most 48 characters. Count them.\n"
    "2. Rewrite, never truncate. No trailing ellipsis, ever.\n"
    "3. Keep the concrete noun or number. If the fact says $3M or "
    "60-67%, the headline says it too.\n"
    "4. Never state a number the fact does not contain.\n"
    "5. Sentence case. No full stop at the end.\n"
    "6. State the thing. Never address the reader, never instruct "
    "them, never begin with Remember or Ask or Check.\n"
    "7. Use no dashes of any kind. A comma or two sentences instead.\n\n"
    "Good, from real facts:\n"
    '  "Zero data retention. No exceptions."\n'
    '  "60-67% small firms is the sweet spot"\n'
    '  "Privacy is the differentiator"\n'
    '  "The business plan"\n\n'
    "Return raw JSON only, no prose and no code fence, exactly:\n"
    '{"headlines": [{"id": "<the id given>", "headline": "<text>"}]}\n'
)


def build_user_content(patches: Iterable[Dict[str, Any]]) -> str:
    """One call per meeting rather than one per patch.

    Batched because prompt real estate is zero sum (doc 19.5) and
    because a model that sees the siblings writes headlines that differ
    from each other, which a per-patch call cannot do.
    """
    lines = ["Facts to headline:"]
    for patch in patches:
        value = patch.get("value") if isinstance(patch.get("value"), dict) else {}
        text = (value.get("text") or "").strip()
        if not text:
            continue
        lines.append(f'- id: {patch.get("patch_id")}')
        lines.append(f'  type: {patch.get("patch_type")}')
        lines.append(f'  fact: {text}')
    return "\n".join(lines)


def parse(content: Any, patches: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """{patch_id: headline} for the ones that passed, plus the refusals.

    A refused headline is simply absent. It costs that patch a tile,
    which is the correct price: repairing it would mean truncating, and
    a truncated headline is what this whole module exists to avoid.
    """
    facts = {}
    for patch in patches:
        value = patch.get("value") if isinstance(patch.get("value"), dict) else {}
        facts[str(patch.get("patch_id"))] = (value.get("text") or "").strip()

    rows = []
    if isinstance(content, dict):
        rows = content.get("headlines") or []
    accepted: Dict[str, str] = {}
    refused: Dict[str, int] = {}

    for row in rows:
        if not isinstance(row, dict):
            continue
        pid = str(row.get("id") or "").strip()
        if pid not in facts:
            # A hallucinated id, on the resolved_commitments pattern:
            # downgrade rather than trust it.
            refused["unknown_id"] = refused.get("unknown_id", 0) + 1
            continue
        headline = (row.get("headline") or "").strip().strip('"')
        reason = why_invalid(headline, facts[pid])
        if reason:
            refused[reason] = refused.get(reason, 0) + 1
            continue
        accepted[pid] = headline

    return {"headlines": accepted, "refused": refused}


# --------------------------------------------------------------------
# Which patches still need a line
# --------------------------------------------------------------------
#
# THE QUERY LIVES HERE SO A TEST CAN EXECUTE IT. The first version of
# this was written inline in the worker and filtered on `cp.user_id`, a
# column context_patches does not have: migration 26 dropped it and
# `patch_subjects` carries the link. Every test covering the lane read
# SOURCE, because worker.py cannot be imported without asyncpg, so all
# of them passed. The lane never raises by design, so in production the
# error would have been swallowed and it would have written zero
# headlines forever while logging a warning nobody reads.
#
# That is the `Body` import shape a second time: a check that is
# satisfied by the text being present and cannot see whether the text
# means anything. A string cannot be wrong out loud; only an execution
# can. So the SQL is built here, both callers use it, and the DB test
# runs it against a real Postgres.

PENDING_SELECT = """
    SELECT cp.patch_id, cp.patch_type, cp.value, cp.origin_id,
           cp.completed_at, cp.sensitivity
      FROM context_patches cp
      JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
     WHERE cp.status = 'active'
       AND cp.value->>'headline' IS NULL
"""


def build_pending_fetch(subject_key: Optional[str] = None,
                        origin_id: Optional[str] = None):
    """SQL and args for patches that have no headline yet.

    Idempotent by QUERY rather than by bookkeeping: it asks for rows
    without a headline, so a re-ingest, a retry and the backfill can all
    cross the same meeting without paying twice or overwriting a line
    that is already there.

    Eligibility beyond this is NOT decided here. `why_not_a_tile` is the
    single source of truth for what can earn a tile, and restating any
    part of it in SQL would be a second source that drifts toward paying
    a model to headline patches the quilt can never show.
    """
    sql, args = PENDING_SELECT, []
    if subject_key is not None:
        args.append(subject_key)
        sql += f"       AND ps.subject_key = ${len(args)}\n"
    if origin_id is not None:
        args.append(origin_id)
        sql += f"       AND cp.origin_id = ${len(args)}\n"
    sql += "     ORDER BY cp.created_at DESC\n"
    return sql, args
