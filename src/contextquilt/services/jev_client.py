"""TypeSafe's Jev: one typed judgment per call, no prose, input billed only.

WHY A SECOND PROVIDER. Measured 2026-09-22 (docs/experiments/
2026-09-22-jev-second-scorer.md): on conduct versus character, the shipped
guardrail scored 9 of 16 and Jev scored 16 of 16, and the eight items the
denylist could never see include every Spanish and Portuguese one, because
`character_word_in` is 29 English adjectives matched word bounded. That is
not a model beating a model. It is a fixed list against something that reads
meaning, and the gap widens with every non English meeting we ingest.

WHAT IT RETURNS. A `noul` (probability a condition holds), a `choice` with a
confidence, or a `score`. Never text. It cannot extract a value, normalize a
date or write a sentence, and its published jaggedness notes say it is
unreliable at counting and at comparing dates, which is doc 19.1 arriving
from the vendor. So nothing on a time axis and nothing that counts is ever
asked here.

FAILS OPEN, ALWAYS. This runs on the write path after the user already has
their response. A timeout, a 500, a missing key or a malformed answer must
leave the pipeline exactly as it was, because the alternative is dropping a
memory over somebody else's outage. Every failure path returns None and the
caller keeps the row.

QUESTIONS ARE BATCHED PER STATE, not per row. One request carries one state
and as many questions as you like, each scored independently, so asking a
second thing about the same line is nearly free. Several LINES still means
several requests; fire them with `asyncio.gather`, never in a loop, or a
meeting's worth costs seconds instead of one round trip.

Kill switch `CQ_JEV_ENABLED=0`. Key `CQ_TYPESAFE_API_KEY`, or Secret Manager
as `typesafe-api-key` when `CQ_GCP_PROJECT` is set.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

import httpx
import structlog

from contextquilt.secrets import get_secret

logger = structlog.get_logger()

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT = 8.0

# The band where a noul is saying it does not know. Treated as a hold by
# every caller rather than rounded to the nearer side: a 0.5 means yes and
# no are equally likely, not that the answer is mildly true.
UNDECIDED_LOW = 0.45
UNDECIDED_HIGH = 0.55


def enabled() -> bool:
    """Read at CALL time. A module-level constant would pin the value to
    whenever this module happened to be imported, which is the difference
    between a knob and a decoration."""
    return (os.getenv("CQ_JEV_ENABLED", "1") or "1").strip().lower() \
        not in ("0", "false", "no")


def model() -> str:
    return os.getenv("CQ_JEV_MODEL") or DEFAULT_MODEL


def api_key() -> str:
    """Empty string when no key is configured anywhere, which reads as off.

    Deliberately not an exception: an unconfigured environment (a test box,
    a fresh clone, an operator who has not added the secret yet) should be
    a no-op, not a crash inside the worker's extraction loop.
    """
    try:
        return get_secret("typesafe-api-key", env_var="CQ_TYPESAFE_API_KEY") or ""
    except Exception:
        return ""


def is_undecided(noul: Optional[float]) -> bool:
    """A missing answer counts as undecided. Silence is not agreement."""
    if noul is None:
        return True
    return UNDECIDED_LOW <= noul <= UNDECIDED_HIGH


def noul_of(answers: Any, name: str) -> Optional[float]:
    """The probability for one question, or None if it is not usable.

    Defensive about the whole shape because the answer is remote: a dict
    that is not a dict, a question that came back empty, a probability that
    is a string all resolve to None, and None means the caller keeps the row.
    """
    if not isinstance(answers, dict):
        return None
    answer = answers.get(name)
    if not isinstance(answer, dict):
        return None
    value = answer.get("noul")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


async def ask(state: Dict[str, Any], questions: Dict[str, Any],
              *, timeout: float = DEFAULT_TIMEOUT) -> Optional[Dict[str, Any]]:
    """One request, one state, N questions. Returns answers, or None.

    None is the single failure signal: disabled, unkeyed, timed out,
    refused, or malformed. Callers never distinguish them for behavior,
    only for the log line.
    """
    if not enabled():
        return None
    key = api_key()
    if not key:
        return None
    body = {"state": state, "model": model(), "questions": questions}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            response = await client.post(
                ENDPOINT, json=body,
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"})
            if response.status_code != 200:
                logger.warning("jev_request_failed",
                               status=response.status_code,
                               detail=response.text[:200])
                return None
            payload = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("jev_request_error", reason=str(exc)[:200])
        return None
    if not isinstance(payload, dict):
        return None
    answers = payload.get("answers")
    return answers if isinstance(answers, dict) else None
