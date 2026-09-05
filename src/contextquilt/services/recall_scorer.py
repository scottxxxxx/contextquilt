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
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


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

# Facet-keyed base priorities for types NOT in TYPE_PRIORITY (a
# registered app's vocabulary; TR's types scored base 0 before this and
# sorted below everything). The values sit inside the SS table's own
# ordering philosophy: intentions and constraints outrank episodes,
# connections outrank self-disclosure. A COMPLETABLE type outranks its
# facet default regardless (see score_patches), mirroring
# commitment/blocker sitting at the top of the SS table. SS names all
# hit TYPE_PRIORITY first, so this table cannot move an SS score.
FACET_PRIORITY: Dict[str, int] = {
    "Intention": 38,
    "Constraint": 36,
    "Episode": 30,
    "Connection": 22,
    "Attribute": 15,
    "Affinity": 10,
}
COMPLETABLE_DEFAULT_PRIORITY = 45

# Conduct rows (origin scoped, not project scoped: SS's moment). Below
# preference (10) and above takeaway (5), so the compact rules about a
# person win the block's slots, and boosted only when the row's OWNER is
# a person the query named, never on a text hit. Persona test 2026-09-05:
# as Episodes at 30 they displaced the rows carrying each person's rules
# and the full block lost to the block without them on every question;
# alone they scored as well as the rules, so the knowledge is real and
# the ranking was the defect.
CONDUCT_PRIORITY = 8


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
# Cue-fetched patches: below the +100 entity-match boost (an explicit
# name is a stronger signal than a topic association) but above the
# 60-point keyword-overlap cap, so an associatively-recalled patch can't
# be stranded below generic keyword matches.
CUE_MATCH_BOOST = 75.0

DEADLINE_OVERDUE_BOOST = 25.0
DEADLINE_DUE_SOON_BOOST = 15.0
DEADLINE_DUE_SOON_WINDOW_DAYS = 7
# The overdue boost expires 30 days past deadline, mirroring the recall
# overdue-guarantee age cap (main.py). A months-stale open item is far
# likelier dead-but-unclosed than urgent; boosting it lets it outrank
# live work through the entity/graph legs even after the guarantee
# stops carrying it (observed on prod: a January-deadline commitment
# still ranking into ABM status renders in July via its entity match).
DEADLINE_OVERDUE_BOOST_MAX_AGE_DAYS = 30

# Salience (value.salience, set at extraction from speaker signals):
# same magnitude band as deadline urgency — a weight modifier, never a
# relevance signal strong enough to beat an entity or cue match.
SALIENCE_HIGH_BOOST = 20.0
SALIENCE_LOW_PENALTY = -10.0


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
    "mis", "tus", "sus", "quill", "ellos", "ellas", "nosotros",
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


def _patch_salience(row: Any) -> "str | None":
    """The stored `value.salience` level ('low'/'high'), None otherwise."""
    v = row["value"] if isinstance(row, dict) else row["value"]
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return None
    if not isinstance(v, dict):
        return None
    raw = v.get("salience")
    return raw if raw in ("low", "high") else None


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


def _owner_matches(owner_lower: str, name_lower: str) -> bool:
    """The owner IS this person: same name, or the same first token
    (a bare first name matched in the query against a full owner name,
    or the reverse). Never a substring."""
    if owner_lower == name_lower:
        return True
    o, n = owner_lower.split(" ")[0], name_lower.split(" ")[0]
    return bool(o) and o == n


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


def _freshness_multiplier(
    patch_type: str, last_observed: float, now: float,
    freshness_types: frozenset = FRESHNESS_TRACKED_TYPES,
) -> float:
    """Multiplicative staleness penalty for self-typed patches.

    Returns 1.0 for any patch type outside FRESHNESS_TRACKED_TYPES,
    or for any patch missing a timestamp. Otherwise returns
    `max(FRESHNESS_FLOOR, exp(-days_stale / FRESHNESS_TIME_CONSTANT_DAYS))`.
    """
    if patch_type not in freshness_types:
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
    cue_matched_patch_ids: "Optional[set]" = None,
    facet_by_type: "Optional[Dict[str, str]]" = None,
    freshness_types: "Optional[frozenset]" = None,
    deadline_types: "Optional[frozenset]" = None,
    completable_types: "Optional[frozenset]" = None,
    conduct_types: "Optional[frozenset]" = None,
) -> List[Tuple[float, Any]]:
    """Score each patch against the query.

    Returns a list of (score, row) tuples sorted high-to-low.

    Scoring components (higher = more relevant):
      base type priority             5..50
      entity-match boost             +100 per matched entity appearing in text
      cue-match boost                +CUE_MATCH_BOOST when the patch was
                                     fetched via a matched cue — cue-recalled
                                     patches often share no words with the
                                     query (that's what cues are for), so
                                     keyword overlap can't rank them
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
    # The optional sets come from the facet runtime (registered-manifest
    # facts); the defaults keep every existing caller byte-identical.
    # For SS the runtime sets equal these constants (pinned floor).
    facet_by_type = facet_by_type or {}
    if freshness_types is None:
        freshness_types = FRESHNESS_TRACKED_TYPES
    if deadline_types is None:
        deadline_types = DEADLINE_BOOSTED_TYPES
    completable_types = completable_types or frozenset()
    conduct_types = conduct_types or frozenset()

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

        is_conduct = patch_type in conduct_types
        if is_conduct:
            base = CONDUCT_PRIORITY
        elif patch_type in TYPE_PRIORITY:
            base = TYPE_PRIORITY[patch_type]
        elif patch_type in completable_types:
            base = COMPLETABLE_DEFAULT_PRIORITY
        else:
            base = FACET_PRIORITY.get(facet_by_type.get(patch_type, ""), 0)
        score = float(base)

        # Entity-match boost. A conduct row is boosted by its OWNER only:
        # "Asked Hassan whether Tripp was joining" owned by Raj is about
        # Raj, and a text hit on Hassan would surface it for the wrong
        # person.
        if is_conduct:
            owner_lower = _patch_owner(row).lower()
            for name in entity_names_lower:
                if name and owner_lower and _owner_matches(owner_lower, name):
                    score += 100.0
                    break
        else:
            for name in entity_names_lower:
                if name and name in text_lower:
                    score += 100.0

        # Cue-match boost — patch was fetched because a cue attached to it
        # appeared in the query text
        if cue_matched_patch_ids:
            pid = row["patch_id"] if isinstance(row, dict) else row.get("patch_id")
            if pid is not None and str(pid) in cue_matched_patch_ids:
                score += CUE_MATCH_BOOST

        # Query keyword overlap
        if query_words:
            patch_words = set(_keywords(text))
            overlap = len(query_words & patch_words)
            score += min(overlap * 15.0, 60.0)

        # Deadline urgency boost — overdue/due-today actionable items
        # float above otherwise-equal patches; imminent ones get a
        # smaller bump. Far-future deadlines get nothing.
        if patch_type in deadline_types:
            deadline_d = _patch_deadline_date(row)
            if deadline_d is not None:
                days_until = (deadline_d - today).days
                if -DEADLINE_OVERDUE_BOOST_MAX_AGE_DAYS <= days_until <= 0:
                    score += DEADLINE_OVERDUE_BOOST
                elif 0 < days_until <= DEADLINE_DUE_SOON_WINDOW_DAYS:
                    score += DEADLINE_DUE_SOON_BOOST

        # Salience weight — extraction-time judgment of how strongly the
        # speaker signaled this. A modifier, deliberately weaker than any
        # relevance signal (entity/cue match).
        salience = _patch_salience(row)
        if salience == "high":
            score += SALIENCE_HIGH_BOOST
        elif salience == "low":
            score += SALIENCE_LOW_PENALTY

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
        score *= _freshness_multiplier(patch_type, ts, now, freshness_types)

        scored.append((score, row))

    scored.sort(key=lambda t: t[0], reverse=True)
    return scored


def top_k_patches(
    scored_patches: Sequence[Tuple[float, Any]],
    k: int,
) -> List[Any]:
    """Return the top K patches from a scored list (highest score first)."""
    return [row for _, row in scored_patches[: max(0, k)]]
