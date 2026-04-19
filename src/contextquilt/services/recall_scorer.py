"""
Recall scoring — ranks patches by relevance to a query.

Replaces the earlier type-priority-only heuristic with a composite score
that combines:
  - Entity-match boost (patch text contains a matched entity name)
  - Query keyword overlap (patch text shares content words with the query)
  - Type priority (actionable types float up)
  - Recency (newer patches get a small boost)

Designed for the hot path — pure Python, no LLM calls. Target: <5ms on
a few hundred patches.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Sequence, Tuple


# Type priority — higher means "more likely to be relevant at recall time".
# Actionable work items float above passive observations.
TYPE_PRIORITY: Dict[str, int] = {
    "commitment": 50,
    "blocker": 45,
    "decision": 40,
    "goal": 38,
    "constraint": 36,
    "event": 32,
    "role": 30,
    "person": 25,
    "org": 22,
    "project": 20,
    "trait": 15,
    "preference": 10,
    "takeaway": 5,
}


# Stopwords filtered out of query keyword matching. Kept small — we want
# moderate filtering, not aggressive NLP. Project names like "app" or
# "plan" should still count as content words when they appear in a query.
_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "and", "or", "but", "if", "then", "else", "so",
    "of", "in", "on", "at", "by", "for", "to", "from", "with", "as", "into",
    "this", "that", "these", "those", "it", "its",
    "i", "you", "he", "she", "we", "they", "me", "him", "her", "us", "them",
    "my", "your", "his", "our", "their",
    "do", "does", "did", "done", "have", "has", "had",
    "what", "which", "who", "whom", "whose", "when", "where", "why", "how",
    "can", "could", "should", "would", "will", "may", "might",
    "not", "no", "yes", "just", "about", "any", "all", "some",
})


_WORD_RE = re.compile(r"[a-z0-9']+")


def _keywords(text: str) -> List[str]:
    """Lowercase, split, drop stopwords + very-short tokens."""
    if not text:
        return []
    return [
        w for w in _WORD_RE.findall(text.lower())
        if w not in _STOPWORDS and len(w) > 2
    ]


def _patch_text(row: Any) -> str:
    """Pull the display text out of a patch row's JSON value."""
    v = row["value"] if isinstance(row, dict) else row["value"]
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return ""
    if isinstance(v, dict):
        return v.get("text", "") or ""
    return ""


def _patch_owner(row: Any) -> str:
    v = row["value"] if isinstance(row, dict) else row["value"]
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return ""
    if isinstance(v, dict):
        return v.get("owner", "") or ""
    return ""


def _created_at(row: Any) -> float:
    ts = row.get("created_at") if isinstance(row, dict) else getattr(row, "created_at", None)
    if not ts:
        return 0.0
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0
    if isinstance(ts, datetime):
        return ts.timestamp()
    return 0.0


def score_patches(
    patches: Sequence[Any],
    query_text: str,
    matched_entity_names: Iterable[str],
) -> List[Tuple[float, Any]]:
    """Score each patch against the query.

    Returns a list of (score, row) tuples sorted high-to-low.

    Scoring components (higher = more relevant):
      base type priority             5..50
      entity-match boost             +100 per matched entity appearing in text
      query keyword overlap          +15 per overlapping keyword (capped at 60)
      recency (0..10)                +10 for newest, decaying to 0 as rows age
    """
    query_words = set(_keywords(query_text))
    entity_names_lower = [n.lower() for n in matched_entity_names if n]

    # Find the freshest timestamp in the set for normalized recency scoring
    newest = 0.0
    oldest = 0.0
    timestamps = [_created_at(r) for r in patches]
    if timestamps:
        newest = max(timestamps)
        oldest = min(timestamps)

    scored: List[Tuple[float, Any]] = []
    for row in patches:
        patch_type = (row["patch_type"] if isinstance(row, dict) else row.get("patch_type")) or ""
        text = _patch_text(row)
        text_lower = text.lower()

        score = float(TYPE_PRIORITY.get(patch_type, 0))

        # Entity-match boost
        for name in entity_names_lower:
            if name and name in text_lower:
                score += 100.0

        # Query keyword overlap
        if query_words:
            patch_words = set(_keywords(text))
            overlap = len(query_words & patch_words)
            score += min(overlap * 15.0, 60.0)

        # Recency — normalized to 0..10 across the current patch batch
        ts = _created_at(row)
        if newest > oldest:
            norm = (ts - oldest) / (newest - oldest)
            score += norm * 10.0
        else:
            score += 5.0  # single patch / identical timestamps — neutral

        scored.append((score, row))

    scored.sort(key=lambda t: t[0], reverse=True)
    return scored


def top_k_patches(
    scored_patches: Sequence[Tuple[float, Any]],
    k: int,
) -> List[Any]:
    """Return the top K patches from a scored list (highest score first)."""
    return [row for _, row in scored_patches[: max(0, k)]]
