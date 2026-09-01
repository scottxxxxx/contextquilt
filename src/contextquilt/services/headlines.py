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

from contextquilt.services.woven_digest import patch_value

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
PLACEHOLDER_NAME = "diarization_placeholder"

# Reasons a BATCH produced nothing, as opposed to a single line being
# refused. Separate names because "the model answered in prose", "the
# model returned an empty list" and "we asked about nothing" are three
# different bugs and only the last is ours upstream.
NO_JSON = "response_was_not_json"
NOTHING_RETURNED = "model_returned_no_headlines"
NOTHING_ASKED = "prompt_carried_no_facts"

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

# "Speaker 3", "Speaker_10", "Unknown Speaker". A diarization label is
# the transcript's way of saying it does not know who spoke, and it is
# not a name. The rest of the system already holds that ruling:
# `is_placeholder_or_self_person` refuses these out of the entities
# array, and `drop_placeholder_entities` strips them again on the way
# to storage. The headline is the ONE surface where the string is most
# visible to a user, and it was the one place nothing checked, so 18
# live headlines read "Speaker 3 create checklist snapshot by Friday"
# on the most prominent text in the product.
#
# SAME VOCABULARY, DIFFERENT QUESTION, and that is why this is a
# separate expression rather than a call to that predicate. It asks
# "IS this string a placeholder name" and matches on a prefix. A
# headline needs "does this line CONTAIN one", mid sentence, because
# the label arrives inside a written line and never as the whole of it.
#
# Deliberately NOT matching a bare "unknown". The diarization LABEL is
# what is being refused, and "unknown" alone is ordinary English that
# belongs in a headline ("Unknown cause of the latency spike"). A rule
# that ate that would refuse good lines to catch a form that has never
# appeared in the data.
_PLACEHOLDER = re.compile(
    r"\bspeakers?[\s_]*\d+\b|\b(?:unknown|unidentified)\s+speakers?\b",
    re.I)


def _figures(text: str) -> set:
    """Concrete figures in a string. A SPEAKER NUMBER IS NOT ONE.

    Found an hour after the placeholder rule shipped, by running the
    cleanup it was written for. The rule tells the model to drop
    "Speaker 3" and write "Create checklist snapshot by Friday". The
    model did exactly that, and then this function found the figure `3`
    in the fact, found none in the headline, and refused the correct
    answer as `concrete_figure_dropped`. 16 of the 18 rewrites died
    that way.

    So the two rules were each right and their intersection was a wall:
    one required the label gone, the other required its digit kept, and
    no line can satisfy both. Stripping the label here is what makes
    the placeholder rule able to produce a tile instead of only a
    refusal.

    Done inside `_figures` rather than at the call site so a later
    caller cannot forget. On a headline it is a no-op, because a
    headline carrying a label has already been refused by the time
    figures are counted.
    """
    cleaned = _PLACEHOLDER.sub(" ", text or "")
    return {m.group(0).strip().rstrip(".,") for m in _FIGURE.finditer(cleaned)}


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
    if _PLACEHOLDER.search(text):
        return PLACEHOLDER_NAME

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
    "7. Use no dashes of any kind. A comma or two sentences instead.\n"
    "8. Never write a transcript speaker label. If the fact says "
    "Speaker 3 or Unknown Speaker, that is the transcript saying it "
    "does not know who spoke, not a name. Write the line without "
    "the actor: \"Create checklist snapshot by Friday\", never "
    "\"Speaker 3 create checklist snapshot by Friday\".\n\n"
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
        value = patch_value(patch)
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
        value = patch_value(patch)
        facts[str(patch.get("patch_id"))] = (value.get("text") or "").strip()

    accepted: Dict[str, str] = {}
    refused: Dict[str, int] = {}
    retryable: List[Dict[str, str]] = []

    # AN EMPTY RESULT MUST NAME ITSELF. The first prod dry run returned
    # "0 headlines, 0 refused, $0.00", which reads as "nothing to do"
    # and was actually a total failure: `value` arrived as a JSON string,
    # every patch looked textless, and the prompt went out with no facts
    # in it at all. Nothing in the output distinguished that from a
    # healthy batch of zero.
    #
    # This is the `dropped` argument one level down, and it is the third
    # time tonight the same silence has cost something: an empty answer
    # with a reason and an empty answer without are different states.
    rows = []
    if not isinstance(content, dict):
        refused[NO_JSON] = 1
    else:
        rows = content.get("headlines") or []
        if not rows and facts:
            refused[NOTHING_RETURNED] = 1
    if not facts:
        refused[NOTHING_ASKED] = 1

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
            # An attempt that broke a rule is worth ONE more try with
            # the rule quoted back at it. Measured on the hard residue,
            # patches already refused once: 16% accepted on a single
            # pass, 52% after a retry that names the failure. Models
            # count characters badly and REVISE well, which is the whole
            # reason this beats a stricter first instruction. A "six
            # words" variant was tried first and did worse, 6% against
            # 8%, because it pushed numbers into words and made the
            # lines longer.
            #
            # Empty attempts are not retryable: there is nothing to
            # quote back and nothing was learned.
            if headline:
                retryable.append({"id": pid, "attempt": headline,
                                  "reason": reason})
            continue
        accepted[pid] = headline

    return {"headlines": accepted, "refused": refused, "retryable": retryable}


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


# --------------------------------------------------------------------
# The second pass
# --------------------------------------------------------------------
#
# NOT A REPAIR, which is the distinction section 6.3 turns on. Nothing
# here shortens a string. The model is shown its own attempt, told the
# rule it broke and how long the line actually was, and asked to write a
# different one. Rewriting is what 6.3 demands; truncating is what it
# forbids, and a retry that cut the tail would be the forbidden thing
# wearing a second call's clothes.

RETRY_SYSTEM = (
    "You wrote headlines that broke a hard rule. Rewrite ONLY these.\n\n"
    "Each entry gives your previous attempt and what was wrong with it. "
    "Rewriting means saying LESS, not cutting the end off: drop the "
    "qualifier, drop the second clause, keep the one thing a person "
    "would recognise. Never truncate and never add an ellipsis.\n\n"
    "Rules, all still hard:\n"
    "1. At most 48 characters.\n"
    "2. Keep any number or dollar figure the fact contains.\n"
    "3. Never state a number the fact does not contain.\n"
    "4. Sentence case, no full stop, no dashes of any kind.\n"
    "5. State the thing. Never address the reader.\n"
    "6. Never write a transcript speaker label such as Speaker 3. Drop the actor and state what happened.\n\n"
    "Return raw JSON only, no prose and no code fence, exactly:\n"
    '{"headlines": [{"id": "<the id given>", "headline": "<text>"}]}\n'
)


def build_retry_content(retryable: Iterable[Dict[str, str]],
                        patches: Iterable[Dict[str, Any]]) -> str:
    """The failures, each with its own attempt, length and reason.

    The CHARACTER COUNT is stated explicitly because that is the rule
    models break most and the one they cannot check themselves. Telling
    a model its line was 63 characters is information it did not have;
    telling it again that the limit is 48 is not.
    """
    facts = {str(p.get("patch_id")): (patch_value(p).get("text") or "").strip()
             for p in patches}
    lines = ["Rewrite these:"]
    for item in retryable:
        pid = str(item.get("id") or "")
        if pid not in facts:
            continue
        attempt = item.get("attempt") or ""
        lines.append(f"- id: {pid}")
        lines.append(f"  fact: {facts[pid]}")
        lines.append(f"  your attempt ({len(attempt)} characters): {attempt}")
        lines.append(f"  problem: {item.get('reason')}")
    return "\n".join(lines)


def apply_retry(first: Dict[str, Any], second: Dict[str, Any]) -> Dict[str, Any]:
    """Fold a retry's results into the first pass's.

    HERE RATHER THAN IN THE CALLERS, because a sabotage found that
    nothing could see it there. Swapping the merge for an assignment,
    `out["headlines"] = second["headlines"]`, DISCARDS every line the
    first pass got right and keeps only the recovered ones, and the
    whole suite stayed green: the worker tests read source, and the one
    written for this checked the exception path rather than the success
    path. A retry that loses more than it recovers is the shape of every
    optimisation that ships a regression.

    The first pass's headlines SURVIVE. The retry only ever adds.

    Refusals are the SECOND pass's only. A line refused and then
    rewritten was not refused, and counting both would make the log say
    a batch failed twice as often as it did, which matters because the
    refusal counts are the signal for whether the writer is improving.
    """
    merged = dict(first.get("headlines") or {})
    merged.update(second.get("headlines") or {})
    return {
        "headlines": merged,
        "refused": dict(second.get("refused") or {}),
        "recovered": len(second.get("headlines") or {}),
    }


def self_headline(fact: Optional[str]) -> Optional[str]:
    """A fact that is ALREADY a valid headline is the headline.

    Measured 2026-09-01 on the residue: of 123 tileable patches carrying
    no headline, 21 were facts that pass every rule in `why_invalid`
    unchanged. "Kevin Thompson case", "Boland case", "Rivera case". They
    had been through the writer twice and come back empty both times,
    and the model was being asked to improve on a line that was already
    correct.

    THE GATE MAKES THIS A PRODUCT BUG RATHER THAN A COST ONE. A patch
    with no headline is not selected as a tile, so those 21 memories
    could not appear in the quilt at all, no matter how far the user
    paged. Twenty-one is small; the class is not, because every short
    declarative fact this system ever stores lands in it.

    Checked against the SAME validator the model's output faces, so a
    fact adopted here cannot be one the writer would have been refused
    for: same 48 characters, same figure rules, same no-dash, no-
    imperative, no-cut-sentence. Nothing is waived for being original.

    Returns None when the fact cannot serve, which is the signal to
    spend a model call on it.
    """
    # No separate empty check: `why_invalid` already answers EMPTY for a
    # blank line, and a second guard in front of it is a branch no test
    # can distinguish. A sabotage removing it changed nothing, which is
    # the honest signal that it was never load bearing.
    text = (fact or "").strip()
    return text if why_invalid(text, text) is None else None


def partition_by_self_headline(patches: Iterable[Dict[str, Any]]):
    """(already_have_one, need_a_written_line).

    Applied BEFORE the model call, so a fact that is its own headline
    costs nothing and cannot be refused. It also shrinks every batch,
    which is the cheaper half of the same change.
    """
    free: Dict[str, str] = {}
    remaining: List[Dict[str, Any]] = []
    for patch in patches or ():
        line = self_headline(patch_value(patch).get("text"))
        if line:
            free[str(patch.get("patch_id"))] = line
        else:
            remaining.append(patch)
    return free, remaining
