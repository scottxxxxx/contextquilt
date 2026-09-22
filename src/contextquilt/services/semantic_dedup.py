"""
Semantic dedup — LLM judge for the trigram gray zone.

Trigram similarity (pg_trgm) catches near-identical wordings but misses
same-meaning rephrasings: "Deploy API by EOW" vs "Ship API before end
of week" score below the dedup threshold and accumulate as duplicate
patches. Embeddings would need new infra (pgvector + an embedding
provider — Anthropic doesn't offer one); instead, pairs landing in the
gray band between SEMANTIC_DEDUP_FLOOR and the trigram threshold are
judged by ONE batched LLM call per extraction on the cold path, using
the same client the extraction itself just used.

This module is the pure part (prompt, schema, verdict parsing); the DB
flow lives in worker.store_connected_patches, and
scripts/audit_semantic_dupes.py reuses the same judge for historical
cleanup.
"""

from __future__ import annotations

import asyncio
import os

from typing import Any, List, Sequence, Tuple

# Trigram bands. Above the threshold → same fact, no LLM needed (the
# pre-existing fast path). Between floor and threshold → gray zone,
# judged. Below the floor → different enough that even a generous judge
# would rarely merge; not worth the tokens.
TRIGRAM_DEDUP_THRESHOLD = 0.6
SEMANTIC_DEDUP_FLOOR = 0.35

# Self-disclosure types (trait, preference, goal, constraint: the set the
# freshness model tracks, imported so there is one list) restate the same
# disposition in fresh words every time it comes up, and the wordings sit
# BELOW the floor above. Measured 2026-09-04 on the largest prod account:
# of ~390 active self-typed rows, 18 pairs sat in the judged band and 749
# sat between 0.20 and 0.35, where every duplicate the recall block
# rendered twice that night lived ("Values security as a foundational
# concern..." / "Passionate about security and defense-in-depth...",
# similarity 0.30, three cues in common). So these types get a lower
# floor, a second candidate found through SHARED CUES, and an owner
# guard, because a preference held by a colleague must never fold into
# the user's own.
SELF_TYPED_DEDUP_FLOOR = 0.2
from contextquilt.services.recall_scorer import FRESHNESS_TRACKED_TYPES as SELF_DISCLOSURE_TYPES  # noqa: E402


def dedup_floor_for(patch_type: str, self_types=SELF_DISCLOSURE_TYPES) -> float:
    """The trigram floor below which a candidate is not worth the judge."""
    return SELF_TYPED_DEDUP_FLOOR if patch_type in self_types else SEMANTIC_DEDUP_FLOOR


# Candidate queries, here so the write path and the audit script ask the
# same question. $1 subject_key, $2 patch_type, $3 new text, $4 floor.
TRIGRAM_CANDIDATE_SQL = """
    SELECT cp.patch_id, cp.value->>'text' AS existing_text, cp.project_id,
           SIMILARITY(LOWER(cp.value->>'text'), LOWER($3)) AS sim
    FROM context_patches cp
    JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
    WHERE ps.subject_key = $1 AND cp.patch_type = $2
      AND SIMILARITY(LOWER(cp.value->>'text'), LOWER($3)) > $4
      AND COALESCE(cp.status, 'active') = 'active'
    ORDER BY sim DESC
    LIMIT 1
"""

# Same, with the owner guard: $5 is the new patch's owner ('' for the user).
SELF_TRIGRAM_CANDIDATE_SQL = """
    SELECT cp.patch_id, cp.value->>'text' AS existing_text, cp.project_id,
           SIMILARITY(LOWER(cp.value->>'text'), LOWER($3)) AS sim
    FROM context_patches cp
    JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
    WHERE ps.subject_key = $1 AND cp.patch_type = $2
      AND SIMILARITY(LOWER(cp.value->>'text'), LOWER($3)) > $4
      AND COALESCE(cp.value->>'owner', '') = COALESCE($5, '')
      AND COALESCE(cp.status, 'active') = 'active'
    ORDER BY sim DESC
    LIMIT 1
"""

# The cue candidate: the active same-type, same-owner row sharing the most
# cues with the new patch. $4 is the new patch's cue list, $5 its owner.
# `sim` is still reported so the caller's fast path and the judge see the
# same shape a trigram candidate has.
CUE_CANDIDATE_SQL = """
    SELECT cp.patch_id, cp.value->>'text' AS existing_text, cp.project_id,
           SIMILARITY(LOWER(cp.value->>'text'), LOWER($3)) AS sim,
           count(*) AS shared_cues
    FROM context_patches cp
    JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
    JOIN patch_cues pc ON pc.patch_id = cp.patch_id
    WHERE ps.subject_key = $1 AND cp.patch_type = $2
      AND pc.cue = ANY($4::text[])
      AND COALESCE(cp.value->>'owner', '') = COALESCE($5, '')
      AND COALESCE(cp.status, 'active') = 'active'
    GROUP BY cp.patch_id, cp.value, cp.project_id
    ORDER BY shared_cues DESC, sim DESC
    LIMIT 1
"""

# Bound the judge call: extraction caps patches at 12 per meeting, so
# this is a safety net, not an expected limit.
MAX_JUDGE_PAIRS = 24


DEDUP_JUDGE_SYSTEM = """You judge whether two memory statements record the SAME underlying fact.

For each numbered pair, decide same_fact:
- TRUE when both describe the same action, commitment, decision, blocker, or observation, even if phrased differently, in different words, or in different languages ("Deploy the API by end of week" / "Ship the API before Friday").
- TRUE when one adds or refines detail on the same item (a deadline, an owner, a qualifier): that is an update to one fact, not a second fact.
- For a trait, preference, goal or constraint: TRUE only when a reader who knows the first statement would learn nothing from the second, meaning the same disposition about the SAME object and scope ("Values security at every layer" / "Passionate about defense in depth and skeptical of single point fixes"). FALSE when they name different rules, objects, systems, dates or amounts under one theme: "zero data retention with AI providers" and "anonymize everything sent to an LLM" are two rules, "prefers on premises models" and "prefers hardware level environment segregation" are two preferences, "go live on the 16th" and "go live on the 18th" are two constraints, and a disposition is never the same as its opposite.
- FALSE when the action, object, person, or scope differs ("Ship the API" / "Ship the mobile app"), even if the wording is similar.
- FALSE whenever you are unsure. A missed merge is recoverable; a wrong merge silently loses a memory.

Respond with ONLY a JSON object, no prose, no markdown fences, in exactly this shape:
{"verdicts": [{"pair": 0, "same_fact": true}, {"pair": 1, "same_fact": false}]}
with one entry per pair, using each pair's number exactly once."""


DEDUP_JUDGE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdicts"],
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["pair", "same_fact"],
                "properties": {
                    "pair": {"type": "integer"},
                    "same_fact": {"type": "boolean"},
                },
            },
        },
    },
}


def build_dedup_judge_content(pairs: Sequence[Tuple[str, str]]) -> str:
    """Render the numbered pair list for the judge's user content.

    Each pair is (new_text, existing_text). Caller is responsible for
    capping at MAX_JUDGE_PAIRS.
    """
    blocks: List[str] = []
    for i, (new_text, existing_text) in enumerate(pairs):
        a = (new_text or "").strip().replace("\n", " ")[:300]
        b = (existing_text or "").strip().replace("\n", " ")[:300]
        blocks.append(f"PAIR {i}:\nA: {a}\nB: {b}")
    return "\n\n".join(blocks)


def parse_dedup_verdicts(content: Any, n_pairs: int) -> List[bool]:
    """Map the judge's output to a per-pair bool list.

    Defensive by design: anything malformed, missing, out of range, or
    non-boolean resolves to False (insert as a new patch — today's
    behavior). A judge failure must never lose a memory.
    """
    verdicts = [False] * n_pairs
    if not isinstance(content, dict):
        return verdicts
    for v in content.get("verdicts") or []:
        if not isinstance(v, dict):
            continue
        pair = v.get("pair")
        same = v.get("same_fact")
        if isinstance(pair, int) and 0 <= pair < n_pairs and isinstance(same, bool):
            verdicts[pair] = same
    return verdicts


# ---------------------------------------------------------------------------
# The cheap first pass (2026-09-22). Most gray-zone pairs are not close calls.
# ---------------------------------------------------------------------------
#
# WHY A CASCADE AND NOT A SWAP. Measured over the 16 pair fixture, three
# repeats each (docs/experiments/2026-09-22-jev-second-scorer.md): every pair
# that is NOT the same fact scored between 0.01 and 0.15, and seven of the
# eight that ARE scored 0.71 or higher. The eighth sat at 0.50 to 0.55 across
# all three runs, which is the tool saying it does not know, and it is the
# judge prompt's own worked example ("Deploy the API by end of week" against
# "Ship the API before Friday"). A single 0.5 threshold would decide that pair
# by coin flip, so the band between the two floors escalates to the LLM judge
# that already exists rather than being guessed.
#
# THE FLOORS ARE ASYMMETRIC ON PURPOSE, because the errors are. A missed merge
# leaves two rows that a later re-observation can still collapse. A wrong merge
# silently destroys a memory and its origin. So merging needs 0.70 with the
# nearest negative at 0.15, a margin of 0.55, while anything above 0.30 is
# never insert-by-default; it escalates.
#
# WHEN JEV IS OFF OR UNREACHABLE every pair escalates, which is exactly
# today's behavior: one batched LLM call, and a judge failure inserts.

DEDUP_MERGE_FLOOR = 0.70
DEDUP_INSERT_CEILING = 0.30

DEDUP_QUESTION: dict = {
    "same_fact": {
        "type": "noul",
        "instructions": (
            "Do statement_a and statement_b record the SAME underlying fact, "
            "so that a reader who already knows the first learns nothing new "
            "from the second? Different wording, or a different language, "
            "does not make two facts. A different object, person, date, "
            "amount or scope does."
        ),
        "criteria": {
            "true": (
                "Both name the same action on the same object, or one merely "
                "adds detail to the other such as a deadline or an owner."
            ),
            "false": (
                "They name different objects, people, dates, amounts, rules "
                "or scopes, however similar the wording. A disposition is "
                "never the same as its opposite."
            ),
        },
    }
}


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default
    return value if 0.0 < value < 1.0 else default


def merge_floor() -> float:
    return _float_env("CQ_DEDUP_MERGE_FLOOR", DEDUP_MERGE_FLOOR)


def insert_ceiling() -> float:
    return _float_env("CQ_DEDUP_INSERT_CEILING", DEDUP_INSERT_CEILING)


def triage(noul) -> "bool | None":
    """True merge, False insert, None escalate to the LLM judge.

    None for a missing answer too: silence is not a verdict, and the pair
    still deserves the judge it would have had before this pass existed.
    """
    if noul is None:
        return None
    if noul >= merge_floor():
        return True
    if noul <= insert_ceiling():
        return False
    return None


async def prescreen(pairs: "Sequence[Tuple[str, str]]") -> "List[bool | None]":
    """One typed judgment per pair, concurrently. Never raises.

    Returns one entry per pair in order: True merge, False insert, None means
    the caller must escalate that pair. An all-None result is the disabled
    path and costs nothing.
    """
    from contextquilt.services import jev_client  # local: keeps this module
    # importable with no httpx present, as the pure tests rely on.

    if not pairs or not jev_client.enabled() or not jev_client.api_key():
        return [None] * len(pairs)

    async def one(a: str, b: str):
        answers = await jev_client.ask(
            {"statement_a": a, "statement_b": b}, DEDUP_QUESTION)
        return triage(jev_client.noul_of(answers, "same_fact"))

    results = await asyncio.gather(
        *(one(a, b) for a, b in pairs), return_exceptions=True)
    # An exception object is not a verdict. It escalates, like a missing
    # answer, rather than reading as truthy and merging.
    return [r if isinstance(r, bool) else None for r in results]
