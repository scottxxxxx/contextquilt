"""Guardrail 12b in any language: is this line conduct, or a verdict?

THE RULE IS NOT NEW. A behavioral observation cites what a reader could have
watched happen, never what kind of person somebody is. What is new is the
second carrier. The first is `character_word_in`, 29 English adjectives in
`follow_through`, and it does exactly what a list does: it catches the words
somebody wrote down, in the language they wrote them in.

MEASURED 2026-09-22, 16 items, 8 of them Spanish or Portuguese
(docs/experiments/2026-09-22-jev-second-scorer.md): the denylist caught 1 of
8 character statements, the one containing "defensive". Jev caught 8 of 8.
The items were authored for the test, so what is measured is generalization
beyond a closed list, not a production accuracy figure. It is still the
question that matters, because CQ ingests meetings in languages the list has
never seen.

THE LIST STAYS. It is free, instant, needs no network and cannot fail open,
so it runs first and this runs second on whatever survives. Two carriers for
one rule is normally the defect (doc 19.11); here it is deliberate, and the
reason is that they fail in opposite directions. The list cannot see an
unlisted word. This can be unreachable. Neither covers the other's gap.

FAILS OPEN. An unreachable or undecided answer KEEPS the row. A guardrail
that drops a memory because a third party timed out is worse than the verdict
it was trying to stop, and the list has already had its pass.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

import structlog

from contextquilt.services import jev_client

logger = structlog.get_logger()

# Only these types carry conduct. Kept as a frozenset rather than a literal
# so the behavior lane's type name lives in exactly one place.
CHECKED_TYPES = frozenset({"moment", "behavior"})

QUESTION_NAME = "is_character"

# Written with contrast rather than adjectives: a model describes the SHAPE
# of what it is given, so naming the neighbour it gets confused with does the
# work that more description does not (doc 19.8). The language clause is
# load bearing, since the whole point is the rows the English list cannot
# read. No dash anywhere in it: the model copies the punctuation it sees.
CHARACTER_QUESTION: Dict[str, Any] = {
    QUESTION_NAME: {
        "type": "noul",
        "instructions": (
            "Does this line say what KIND of person somebody is, rather than "
            "reporting one specific thing they were observed doing on one "
            "occasion? Judge the line in whatever language it is written."
        ),
        "criteria": {
            "true": (
                "The line characterizes the person as a type or passes a "
                "verdict on them: defensive, thorough, impatient, dismissive, "
                "meticulous, unreliable, difficult."
            ),
            "false": (
                "The line reports a specific observable action taken at a "
                "particular moment, such as asking, declining, reading "
                "something aloud, postponing, or redirecting."
            ),
        },
    }
}


def enabled() -> bool:
    return (os.getenv("CQ_JEV_CHARACTER_ENABLED", "1") or "1").strip().lower() \
        not in ("0", "false", "no")


def threshold() -> float:
    """Above this, the line is a verdict about a person and is dropped.

    0.5 is safe here ONLY because the separation was measured wide: 0.02 to
    0.05 on conduct, 0.77 to 0.79 on character, with at most 0.02 of movement
    across three repeats. A narrower question would need the undecided band
    instead, which is why `is_undecided` is consulted first regardless.
    """
    try:
        value = float(os.getenv("CQ_JEV_CHARACTER_THRESHOLD", "") or 0.5)
    except (TypeError, ValueError):
        return 0.5
    return value if 0.0 < value < 1.0 else 0.5


def text_of(patch: Any) -> Optional[str]:
    """The observation text, or None when this patch is not one to check."""
    if not isinstance(patch, dict) or patch.get("type") not in CHECKED_TYPES:
        return None
    value = patch.get("value")
    if not isinstance(value, dict):
        return None
    text = value.get("text")
    return text if isinstance(text, str) and text.strip() else None


def verdict(noul: Optional[float], limit: Optional[float] = None) -> bool:
    """True when the row should be DROPPED as a character verdict.

    Undecided and unanswered both return False, which keeps the row. The
    ordering matters: a 0.5 is the tool saying it does not know, and rounding
    that up to "drop" would delete memories on a coin flip.
    """
    if jev_client.is_undecided(noul):
        return False
    return noul > (threshold() if limit is None else limit)


async def _verdict_for(text: str) -> bool:
    answers = await jev_client.ask({"observation": text}, CHARACTER_QUESTION)
    return verdict(jev_client.noul_of(answers, QUESTION_NAME))


async def screen(patches: Sequence[dict]) -> Tuple[List[dict], List[dict]]:
    """Split patches into (kept, dropped). Never raises.

    One request per line, fired CONCURRENTLY: Jev batches questions over one
    state, not states over one request, so a loop would cost a meeting's
    worth of round trips in series. Seven observations is the measured
    average per meeting, which is one burst of seven.
    """
    if not enabled() or not jev_client.enabled():
        return list(patches), []
    indexed = [(i, t) for i, p in enumerate(patches)
               if (t := text_of(p)) is not None]
    if not indexed:
        return list(patches), []
    # `return_exceptions=True` means gather NEVER raises: a failure arrives
    # as an exception OBJECT in the results list. That is the whole failure
    # handling, and it is why there is no try/except here. An earlier version
    # had one, and sabotaging it turned no test red, because the branch could
    # not be reached. An unreachable guard reads as protection and is not.
    verdicts = await asyncio.gather(
        *(_verdict_for(text) for _, text in indexed),
        return_exceptions=True)
    for (_, text), result in zip(indexed, verdicts):
        if isinstance(result, BaseException):
            logger.warning("character_check_failed", text=text[:120],
                           reason=str(result)[:200])

    # `is True` is load bearing. An exception object is truthy, so a plain
    # `if is_character` would DROP every row the scorer failed on, which is
    # the opposite of failing open.
    drop_at = {index for (index, _), is_character in zip(indexed, verdicts)
               if is_character is True}
    kept = [p for i, p in enumerate(patches) if i not in drop_at]
    dropped = [p for i, p in enumerate(patches) if i in drop_at]
    return kept, dropped


async def apply(content: dict, *, user_id: str = "", origin: str = "",
                lane: str = "") -> dict:
    """Run the check over `content["patches"]` and record what it dropped.

    Mutates and returns `content`, matching the sanitizer chain it follows,
    so a caller wires it in the same shape as every other guardrail. The
    drop is made AUDIBLE with the texts, because the client cannot build
    this half and a silent guardrail is one nobody can audit.
    """
    patches = content.get("patches")
    if not isinstance(patches, list) or not patches:
        return content
    kept, dropped = await screen(patches)
    if dropped:
        content["patches"] = kept
        content["_character_check_dropped"] = {
            "count": len(dropped),
            "texts": [str((p.get("value") or {}).get("text"))[:160]
                      for p in dropped][:20],
        }
        logger.info("character_check_dropped", user_id=user_id, origin=origin,
                    lane=lane, dropped=len(dropped),
                    texts=content["_character_check_dropped"]["texts"])
    return content
