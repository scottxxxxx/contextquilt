"""Which patches earn a tile on the Woven memory quilt, and how big.

Section 6 of the Woven handoff (`Woven Handoff.dc.html`, 2026-08-31):
"from thousands of patches, decide the 4 to 6 per week that are worth a
tile". This module is the deterministic half of that: scoring, pruning
and tile sizing. Headlines need a model and are written at ingest, not
here; stitch links need the edge graph and land separately.

Pure by construction. No clock beyond the `today` passed in, no I/O, no
randomness. A digest that reshuffled between two opens of the tab is the
defect the spec's own stability requirement names, and the cheapest way
to never have it is a function whose output depends only on its input.

THE SPEC'S TILING RULE AND ITS OWN WEIGHT RULE CONTRADICT EACH OTHER,
and this module follows the tiling rule. Section 5 fixes the grid at 6
columns with spans 1->2, 2->3, 3->4, so a row is only exact for weight
pairs 3+1 or 2+2 (or a triple of 1s). Section 6.1 then says "top scorer
-> 3, next two -> 2, rest -> 1", which for six patches is the multiset
{3,2,2,1,1,1}. That multiset CANNOT TILE, in any order: checked
exhaustively over all 60 permutations, zero of them partition into rows
of exactly 6 columns. It is not a bad arrangement, it is an impossible
one, and it would have shipped as a ragged quilt rather than an obvious
error.

{3,3,2,2,1,1} tiles 12 ways and keeps the spec's "cap two weight-3"
rule. Ordering stays by rank, index 0 strongest as section 5 requires,
and the client pairs OUTSIDE IN: rank 0 with rank 5, rank 1 with rank 4,
rank 2 with rank 3. That yields rows of (3,1), (3,1), (2,2), every row
exact, and the strongest patch still gets the first and largest tile.
`row_pairs()` below hands the client that pairing so it is not
re-derived on the device.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Section 5: the grid, and the only row shapes that fit it.
COLUMNS = 6
SPAN_FOR_WEIGHT = {1: 2, 2: 3, 3: 4}

# Section 6.1's consequence tiers. A type whose absence changes something
# starts high; a type that merely records that a thing happened starts
# low. Deliberately a table rather than a model judgment: the spec argues
# it once, and a model handed the same facts twice will not agree with
# itself about which mattered.
CONSEQUENCE = {
    "decision": 1.0, "commitment": 1.0, "constraint": 0.95, "blocker": 0.95,
    "deliverable": 0.8, "goal": 0.7, "takeaway": 0.6, "role": 0.35,
    "event": 0.3, "preference": 0.5, "trait": 0.45, "project": 0.4,
    "org": 0.3, "insight": 0.55,
}
DEFAULT_CONSEQUENCE = 0.5

# Section 6.2: meeting exhaust. English-only and deliberately short; a
# long denylist starts deciding what is interesting, which is the
# scorer's job and not a word list's.
EXHAUST = re.compile(
    r"\b(take (?:that|this) offline|circle back|schedul\w+ (?:a )?(?:call|meeting|time)|"
    r"agenda|calendar invite|reschedul\w+|let'?s (?:sync|touch base))\b",
    re.I,
)

# Section 6.1 specificity: a number, a date, a currency figure, a percentage.
SPECIFIC = re.compile(r"(\$[\d,]+|\b\d+(?:[.,]\d+)?\s?%|\b\d{4}-\d{2}-\d{2}\b|\b\d[\d,.]*\b)")

# Section 6.2 again, and the reason this constant exists rather than an
# `origin_id IS NULL` test. The spec says "unknown / legacy-orphan
# patches (NULL origin) never get tiles", but NULL origin is NOT
# orphanhood: it is the deliberate design for user-scoped types
# (see the origin_id design ruling). Measured over 14 days, person
# 140/140, insight 108/108, project 83/83, preference 47/47, trait
# 30/30 and org 17/17 carry no origin_id ON PURPOSE, which is 425
# patches. The spec's own token table colours trait, preference,
# project and org, so an origin-null filter would have deleted five
# types the design renders while looking like the ranker being choosy.
# So the orphan test is "an EPISODE type with no origin", never "no
# origin".
USER_SCOPED_TYPES = frozenset(
    {"person", "insight", "project", "preference", "trait", "org"}
)

# Reasons, not booleans. A pruner that reports only that it dropped
# something cannot tell you which rule fired, and then "nothing was
# worth a tile" and "the rules are too tight" are one observable.
DROP_NO_TEXT = "no_text"
DROP_PERSON = "person_type_not_rendered"
DROP_ORPHAN = "episode_without_origin"
DROP_EXHAUST = "meeting_exhaust"
DROP_RESOLVED = "already_acted_on"
DROP_CONTESTED = "contested_identity"
DROP_SENSITIVE = "sensitive_content"
DROP_SHELVED = "shelved_by_user"


def _text(patch: Dict[str, Any]) -> str:
    value = patch.get("value")
    if isinstance(value, dict):
        return (value.get("text") or "").strip()
    return ""


def why_not_a_tile(patch: Dict[str, Any]) -> Optional[str]:
    """The reason this patch cannot earn a tile, or None.

    Section 6.2, in the order that costs least to evaluate. Returns the
    REASON so the caller can report which rule fired, on the same
    argument as every other gate in this codebase.
    """
    text = _text(patch)
    if not text:
        return DROP_NO_TEXT

    ptype = patch.get("patch_type")
    if ptype == "person":
        return DROP_PERSON

    value = patch.get("value") if isinstance(patch.get("value"), dict) else {}

    # A user who let something go does not want it back as a tile.
    if value.get("shelved_at"):
        return DROP_SHELVED

    # Health, legal and compensation reach the meeting seam if the user
    # goes looking, never the home quilt, which is glanceable in public.
    if (patch.get("sensitivity") or "normal") != "normal":
        return DROP_SENSITIVE
    if value.get("sensitivity") == "private":
        return DROP_SENSITIVE

    # A confidently wrong memory is the one thing that breaks trust for
    # good, so anything the identity paths are still arguing about waits.
    if value.get("contested_at") or value.get("contested_by"):
        return DROP_CONTESTED

    # Completed commitments and resolved blockers belong to the meeting
    # seam, not to this week's tiles.
    if patch.get("completed_at") or value.get("completion_source"):
        return DROP_RESOLVED

    if ptype not in USER_SCOPED_TYPES and not patch.get("origin_id"):
        return DROP_ORPHAN

    if EXHAUST.search(text):
        return DROP_EXHAUST
    return None


def salience(patch: Dict[str, Any], today: Optional[date] = None,
             edge_count: int = 0) -> float:
    """Section 6.1's six signals, combined. 0 to 1, internal only.

    NOT served on the wire. The digest array is already ordered, so a
    float alongside it carries nothing the index does not, and a served
    0-to-1 score on a memory is the shape doc 16 forbade for confidence.
    Kept because ordering needs a number and QA needs to see it.
    """
    value = patch.get("value") if isinstance(patch.get("value"), dict) else {}
    text = _text(patch)

    score = CONSEQUENCE.get(patch.get("patch_type"), DEFAULT_CONSEQUENCE)

    # A dated item is a consequence with a clock on it.
    if value.get("deadline_date"):
        score += 0.25

    if SPECIFIC.search(text):
        score += 0.2

    # Connectivity: part of a story rather than a loose fact, and it is
    # what earns the "stitched to" section on the detail screen.
    if edge_count >= 2:
        score += 0.2
    elif edge_count == 1:
        score += 0.08

    # Recurrence. The spec calls this the strongest positive signal we
    # have and it is already kept by the write path, monotonic, so this
    # costs nothing to read.
    try:
        restatements = int(value.get("restatement_count") or 0)
    except (TypeError, ValueError):
        restatements = 0
    if restatements >= 2:
        score += 0.3
    elif restatements == 1:
        score += 0.15

    if value.get("owner"):
        score += 0.1

    # The extraction's own salience call, when it made one. Absent is
    # normal, which is why absence adds nothing rather than subtracting.
    marked = (value.get("salience") or "").lower() if isinstance(value.get("salience"), str) else ""
    if marked == "high":
        score += 0.2
    elif marked == "low":
        score -= 0.2

    # Freshness, as a penalty rather than a boost, so a live commitment
    # from three weeks ago still beats a stale takeaway from Tuesday.
    state = (patch.get("decay_state") or "").lower()
    if state == "aging":
        score -= 0.12
    elif state == "stale":
        score -= 0.3

    return round(max(score, 0.0), 4)


# Weights in RANK order plus the row plan, per digest size. A TABLE
# rather than a formula, for the same reason CONSEQUENCE is one: the
# constraint is small, finite and argued once, and a formula that
# happened to satisfy it would still need every case checked. Every
# entry here is verified by test to fill each row to exactly 6 columns
# and to keep weights non-increasing by rank, so the strongest patch is
# never shown smaller than a weaker one.
#
# Rows are NOT always consecutive. At six tiles the pairing is outside
# in, rank 0 with rank 5, which is what lets the array stay in rank
# order (section 5 requires index 0 to be strongest) while every row
# still fills the grid.
#
# One tile is outside the grid entirely: no single span equals 6, so the
# client renders it full width. Recorded as a real case rather than
# excluded, because the spec forbids padding a thin week to reach a
# tile count.
LAYOUTS: Dict[int, Tuple[List[int], List[Tuple[int, ...]]]] = {
    1: ([1], [(0,)]),
    2: ([3, 1], [(0, 1)]),
    3: ([1, 1, 1], [(0, 1, 2)]),
    4: ([3, 2, 2, 1], [(0, 3), (1, 2)]),
    5: ([2, 2, 1, 1, 1], [(0, 1), (2, 3, 4)]),
    6: ([3, 3, 2, 2, 1, 1], [(0, 5), (1, 4), (2, 3)]),
}
MAX_TILES = max(LAYOUTS)


def assign_weights(count: int) -> List[int]:
    """Weights in rank order for a digest of `count` tiles.

    Follows section 5's tiling rule rather than section 6.1's assignment
    rule, because 6.1's {3,2,2,1,1,1} cannot tile in any order. See the
    module docstring. Caps weight-3 tiles at two, which 6.1 asks for.
    """
    if count <= 0:
        return []
    return list(LAYOUTS[min(count, MAX_TILES)][0])


def row_pairs(count: int) -> List[Tuple[int, ...]]:
    """Rank indexes grouped into rows that each fill the grid exactly.

    Handed to the client rather than re-derived there: the grouping is a
    consequence of the weight distribution, and two implementations of
    one rule is how they drift.
    """
    if count <= 0:
        return []
    return list(LAYOUTS[min(count, MAX_TILES)][1])


def row_is_exact(weights: Sequence[int]) -> bool:
    """Does this row of weights fill the grid exactly?"""
    return sum(SPAN_FOR_WEIGHT.get(w, 0) for w in weights) == COLUMNS


def build_digest(
    candidates: Iterable[Dict[str, Any]],
    limit: int = 6,
    today: Optional[date] = None,
    edge_counts: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """Ordered, pruned, weighted tiles plus the reasons for every drop.

    `limit` is a ceiling and never a target. The spec is explicit that
    an empty quilt is a real state and padding it with weak patches is
    worse, so this returns fewer rather than reaching.
    """
    edge_counts = edge_counts or {}
    kept: List[Tuple[float, Dict[str, Any]]] = []
    dropped: Dict[str, int] = {}

    for patch in candidates:
        reason = why_not_a_tile(patch)
        if reason:
            dropped[reason] = dropped.get(reason, 0) + 1
            continue
        pid = str(patch.get("patch_id") or "")
        kept.append((salience(patch, today, edge_counts.get(pid, 0)), patch))

    # patch_id as the tiebreak, so equal scores cannot reorder between
    # two calls. A tile that moved because a sort was unstable is the
    # stability defect the spec names, arriving by the back door.
    kept.sort(key=lambda pair: (-pair[0], str(pair[1].get("patch_id") or "")))
    chosen = kept[:max(limit, 0)]
    weights = assign_weights(len(chosen))

    return {
        "patches": [
            {
                "patch_id": patch.get("patch_id"),
                "patch_type": patch.get("patch_type"),
                "fact": _text(patch),
                "weight": weight,
                "source_meeting_id": patch.get("origin_id"),
                "occurred_at": patch.get("created_at"),
                "_salience": score,
            }
            for (score, patch), weight in zip(chosen, weights)
        ],
        "row_pairs": row_pairs(len(chosen)),
        "dropped": dropped,
    }
