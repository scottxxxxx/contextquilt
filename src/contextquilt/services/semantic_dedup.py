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

from typing import Any, List, Sequence, Tuple

# Trigram bands. Above the threshold → same fact, no LLM needed (the
# pre-existing fast path). Between floor and threshold → gray zone,
# judged. Below the floor → different enough that even a generous judge
# would rarely merge; not worth the tokens.
TRIGRAM_DEDUP_THRESHOLD = 0.6
SEMANTIC_DEDUP_FLOOR = 0.35

# Bound the judge call: extraction caps patches at 12 per meeting, so
# this is a safety net, not an expected limit.
MAX_JUDGE_PAIRS = 24


DEDUP_JUDGE_SYSTEM = """You judge whether two memory statements record the SAME underlying fact.

For each numbered pair, decide same_fact:
- TRUE when both describe the same action, commitment, decision, blocker, or observation, even if phrased differently, in different words, or in different languages ("Deploy the API by end of week" / "Ship the API before Friday").
- TRUE when one adds or refines detail on the same item (a deadline, an owner, a qualifier) — that is an update to one fact, not a second fact.
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
