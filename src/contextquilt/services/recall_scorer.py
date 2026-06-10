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
import math
import re
from datetime import date, datetime, timezone
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


# Self-typed (you)-speaker patches that get a staleness multiplier.
# Matches the FRESHNESS_TRACKED_TYPES set in src/worker.py:decay_loop
# and the partial index in init-db/20_preference_freshness.sql.
FRESHNESS_TRACKED_TYPES = frozenset({"trait", "preference", "goal", "constraint"})

# Exponential time constant for the staleness multiplier. A patch
# re-observed today scores at multiplier=1.0; the multiplier
# decays as exp(-days_stale / FRESHNESS_TIME_CONSTANT_DAYS), floored
# at FRESHNESS_FLOOR so a still-active-but-aging preference never
# disappears entirely from recall.
#
#   0 days   → 1.00
#   90 days  → 0.78
#   180 days → 0.61
#   365 days → 0.37
#   540 days → 0.30 (floor) — by which point the decay worker
#              archives the patch under the 540d TTL anyway.
FRESHNESS_TIME_CONSTANT_DAYS = 365.0
FRESHNESS_FLOOR = 0.30


# Deadline urgency boost — applied to completable actionable types
# (commitment, blocker) carrying a structured `value.deadline_date`.
# Overdue / due-today items outrank otherwise-equal patches; imminent
# ones get a smaller bump. Measured against the same day-bucketed clock
# as the freshness multiplier, so the boost flips only at UTC midnight
# (prompt-cache stable).
DEADLINE_BOOSTED_TYPES = frozenset({"commitment", "blocker"})
DEADLINE_OVERDUE_BOOST = 25.0
DEADLINE_DUE_SOON_BOOST = 15.0
DEADLINE_DUE_SOON_WINDOW_DAYS = 7


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
    # Spanish function words (3+ chars — shorter ones are already dropped
    # by the length filter in _keywords). Collision-checked against
    # English: "son", "era", and "todo" are deliberately NOT listed —
    # they're content words in English ("my son", "the era", "todo list").
    "los", "las", "una", "unos", "unas", "del", "que", "quien", "quién",
    "ser", "estar", "esta", "está", "están", "estoy",
    "por", "para", "con", "sin", "como", "cómo", "más", "menos", "pero",
    "mis", "tus", "sus", "ella", "ellos", "ellas", "nosotros",
    "usted", "ustedes", "les",
    "este", "estos", "estas", "ese", "esa", "eso", "esos", "esas",
    "aquí", "hay", "muy", "también", "cuando", "cuándo",
    "donde", "dónde", "qué", "porque", "entonces",
    "toda", "todos", "todas", "algo", "nada",
    "hace", "hacer", "tiene", "tener", "tengo",
    "bueno", "bien", "luego", "ahora",
})


# \w is unicode-aware in Python 3, so accented words tokenize whole
# ("jardín" stays "jardín" instead of splitting into "jard" + "n").
# Underscore is excluded to keep snake_case identifiers splitting the
# way the previous [a-z0-9']+ pattern did.
_WORD_RE = re.compile(r"[^\W_]+(?:'[^\W_]+)*")


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


def _patch_deadline_date(row: Any) -> "date | None":
    """Parse the structured `value.deadline_date` (YYYY-MM-DD) if present."""
    v = row["value"] if isinstance(row, dict) else row["value"]
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return None
    if not isinstance(v, dict):
        return None
    raw = v.get("deadline_date")
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


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


def _to_epoch(ts: Any) -> float:
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


def _created_at(row: Any) -> float:
    ts = row.get("created_at") if isinstance(row, dict) else getattr(row, "created_at", None)
    return _to_epoch(ts)


def _last_observed_at(row: Any) -> float:
    """Freshness anchor for self-typed patches. Falls back to created_at
    so pre-migration rows behave sensibly even if the backfill hasn't
    landed yet."""
    ts = row.get("last_observed_at") if isinstance(row, dict) else getattr(row, "last_observed_at", None)
    epoch = _to_epoch(ts)
    if epoch > 0.0:
        return epoch
    return _created_at(row)


def _freshness_multiplier(patch_type: str, last_observed: float, now: float) -> float:
    """Multiplicative staleness penalty for self-typed patches.

    Returns 1.0 for any patch type outside FRESHNESS_TRACKED_TYPES,
    or for any patch missing a timestamp. Otherwise returns
    `max(FRESHNESS_FLOOR, exp(-days_stale / FRESHNESS_TIME_CONSTANT_DAYS))`.
    """
    if patch_type not in FRESHNESS_TRACKED_TYPES:
        return 1.0
    if last_observed <= 0.0 or now <= last_observed:
        return 1.0
    days_stale = (now - last_observed) / 86400.0
    decayed = math.exp(-days_stale / FRESHNESS_TIME_CONSTANT_DAYS)
    return max(FRESHNESS_FLOOR, decayed)


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
      deadline urgency               +25 overdue/due-today, +15 due within 7d
                                     (commitment/blocker with deadline_date)
      recency (0..10)                +10 for newest, decaying to 0 as rows age
      freshness multiplier           applied last for trait/preference/goal/
                                     constraint based on last_observed_at —
                                     scales the running total down to a
                                     floor of FRESHNESS_FLOOR for very stale
                                     self-typed patches.
    """
    query_words = set(_keywords(query_text))
    entity_names_lower = [n.lower() for n in matched_entity_names if n]

    # Recency normalization uses last_observed_at when available
    # (freshness anchor for self-typed patches), falling back to
    # created_at for non-freshness-tracked types.
    newest = 0.0
    oldest = 0.0
    timestamps = [_last_observed_at(r) for r in patches]
    if timestamps:
        newest = max(timestamps)
        oldest = min(timestamps)

    # Bucket `now` to the UTC day so back-to-back recall calls produce
    # byte-identical scores. Upstream prompt caching (RECALL_RENDER_CACHE_TTL
    # in main.py + Anthropic cache_control) depends on the rendered context
    # being stable across the cache window; freshness penalties stepping
    # by the second would break that. The freshness multiplier itself is
    # measured in days so day-grain is the natural quantization.
    now = float(int(datetime.now(timezone.utc).timestamp() // 86400) * 86400)
    today = datetime.fromtimestamp(now, tz=timezone.utc).date()

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

        # Deadline urgency boost — overdue/due-today actionable items
        # float above otherwise-equal patches; imminent ones get a
        # smaller bump. Far-future deadlines get nothing.
        if patch_type in DEADLINE_BOOSTED_TYPES:
            deadline_d = _patch_deadline_date(row)
            if deadline_d is not None:
                days_until = (deadline_d - today).days
                if days_until <= 0:
                    score += DEADLINE_OVERDUE_BOOST
                elif days_until <= DEADLINE_DUE_SOON_WINDOW_DAYS:
                    score += DEADLINE_DUE_SOON_BOOST

        # Recency — normalized to 0..10 across the current patch batch
        ts = _last_observed_at(row)
        if newest > oldest:
            norm = (ts - oldest) / (newest - oldest)
            score += norm * 10.0
        else:
            score += 5.0  # single patch / identical timestamps — neutral

        # Freshness multiplier — only affects self-typed patches.
        # Applied last so the multiplier scales the full composite
        # score, not just the recency component.
        score *= _freshness_multiplier(patch_type, ts, now)

        scored.append((score, row))

    scored.sort(key=lambda t: t[0], reverse=True)
    return scored


def top_k_patches(
    scored_patches: Sequence[Tuple[float, Any]],
    k: int,
) -> List[Any]:
    """Return the top K patches from a scored list (highest score first)."""
    return [row for _, row in scored_patches[: max(0, k)]]
