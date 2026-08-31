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

import json
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


def patch_value(patch: Dict[str, Any]) -> Dict[str, Any]:
    """The patch's value as a dict, whatever the driver handed back.

    `value` is JSONB and asyncpg returns it as a JSON STRING unless a
    codec is registered, which this pool does not do. An earlier version
    of this module checked `isinstance(value, dict)` and returned empty
    for anything else, so EVERY patch dropped as `no_text` and the quilt
    rendered empty for every user. Caught on real data in one run
    because `dropped` reports the rule that fired: 351 candidates, 351
    `no_text`. Elsewhere in the codebase this is already handled with
    `row["value"] if isinstance(row["value"], str)`; this module simply
    did not know.
    """
    value = patch.get("value")
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


# The private name kept as an alias: main.py's seam route and the tests
# already call it, and renaming a helper is not what this fix is about.
_value = patch_value


def _text(patch: Dict[str, Any]) -> str:
    return (_value(patch).get("text") or "").strip()


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

    value = _value(patch)

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
    value = _value(patch)
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


# THE LAYOUT IS TAKEN FROM THE PROTOTYPE, not from the handoff's prose,
# because the prototype is the thing Scott wants it to look like and the
# two disagree in three places.
#
# `Memory Quilt.dc.html` renders a 6-column grid with spans [3,3,2,4,2,4]
# and heights [118,118,96,96,104,104]. Read as rows that is:
#
#     row 1   span 3 + 3   both 118px
#     row 2   span 2 + 4   both  96px
#     row 3   span 2 + 4   both 104px
#
# Three things follow, and each contradicts the written handoff.
#
# ONE. The span multiset maps to weights {3,3,2,2,1,1}, which is what
# this module already derived when section 6.1's {3,2,2,1,1,1} turned
# out not to tile. The prototype independently confirms the correction.
#
# TWO. HEIGHT IS A PROPERTY OF THE ROW, NOT OF THE WEIGHT. Section 4.2's
# `tileHeight` computes height from weight (3->118, 2->104, 1->96),
# which would put a 118 next to a 96 in the same row and produce a
# ragged quilt. The prototype gives every tile in a row the same height
# and varies height BETWEEN rows. That is the look; the computed version
# is not.
#
# THREE, and this is a product decision rather than a bug. In the
# prototype, TILE SIZE DOES NOT ENCODE IMPORTANCE. The spans are a fixed
# decorative pattern applied by position, so the fourth tile is larger
# than the first. Section 5 says index 0 is the strongest and takes the
# first tile, which is still true: first, not biggest. Anyone who later
# wants size to mean rank has to change the pattern, and the quilt will
# stop looking like this.
#
# Rows are consecutive here, unlike the outside-in pairing this module
# used before reading the prototype. Consecutive is simpler and it is
# what the artifact does.
LAYOUTS: Dict[int, Dict[str, Any]] = {
    # tiles: (spans by position, row heights, row groupings)
    1: {"spans": [6], "heights": [118], "rows": [(0,)]},
    2: {"spans": [3, 3], "heights": [118, 118], "rows": [(0, 1)]},
    3: {"spans": [2, 2, 2], "heights": [104, 104, 104], "rows": [(0, 1, 2)]},
    4: {"spans": [3, 3, 2, 4], "heights": [118, 118, 96, 96],
        "rows": [(0, 1), (2, 3)]},
    5: {"spans": [3, 3, 2, 2, 2], "heights": [118, 118, 104, 104, 104],
        "rows": [(0, 1), (2, 3, 4)]},
    6: {"spans": [3, 3, 2, 4, 2, 4],
        "heights": [118, 118, 96, 96, 104, 104],
        "rows": [(0, 1), (2, 3), (4, 5)]},
}
MAX_TILES = max(LAYOUTS)

# The FLOOR on how many tiles one patch type may take. Two, matching
# section 6.1's own cap on weight-3 tiles and for the same stated
# reason: rhythm. The effective cap rises when the week offers few
# types, because a cap that cannot be met is not a cap, it is just a
# reordering. A judgment call, one constant, easily reverted; see the
# note in `build_digest`.
MIN_TILES_PER_TYPE = 2

# The handoff's mapping, kept so a weight can still be reported for
# clients that reason in those terms. Derived FROM the span rather than
# the other way round, because the span is what the prototype fixes.
WEIGHT_FOR_SPAN = {2: 1, 3: 2, 4: 3, 6: 3}


def layout(count: int) -> Dict[str, Any]:
    """Spans, row heights and row groupings for a digest of `count` tiles.

    A table rather than a formula: the constraint is small and finite,
    every entry is verified by test to fill each row to exactly 6
    columns and to hold one height per row, and a formula that happened
    to satisfy both would still need every case checked.
    """
    if count <= 0:
        return {"spans": [], "heights": [], "rows": []}
    return LAYOUTS[min(count, MAX_TILES)]


def assign_weights(count: int) -> List[int]:
    """Weights in DISPLAY order, derived from the prototype's spans."""
    return [WEIGHT_FOR_SPAN[s] for s in layout(count)["spans"]]


def row_pairs(count: int) -> List[Tuple[int, ...]]:
    """Index groupings, one per rendered row.

    Handed to the client rather than re-derived there: the grouping is a
    consequence of the span pattern, and two implementations of one
    layout rule is how they drift.
    """
    return [tuple(r) for r in layout(count)["rows"]]


def row_is_exact(weights: Sequence[int]) -> bool:
    """Does this row of weights fill the grid exactly?"""
    return sum(SPAN_FOR_WEIGHT.get(w, 0) for w in weights) == COLUMNS


def row_spans_exact(spans: Sequence[int]) -> bool:
    """Same question asked of spans, which is what the prototype fixes."""
    return sum(spans) == COLUMNS


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

    # TYPE RHYTHM, and this is a judgment call rather than a rule from
    # the spec, so it is labelled as one and is one constant to revert.
    #
    # Section 6.1 caps weight-3 tiles at two "so the quilt has rhythm",
    # which establishes that visual rhythm is a legitimate selection
    # concern rather than a purity violation. Colour is the same
    # argument: type drives the fabric hue, so an unconstrained ranking
    # on a commitment-heavy week returns five commitments and the quilt
    # renders as one purple block. Measured on real data, the top six
    # were four commitments and two blockers.
    #
    # THE CAP IS DERIVED, NOT FIXED, and the first version got this
    # wrong. A flat cap of two deferred four commitments on a week that
    # held only two types, then backfilled two of them anyway, so the
    # result was four commitments and the cap had done nothing but
    # shuffle. A cap only means something if the material can meet it:
    # with two types and six tiles the honest spread is three and three.
    # So the cap is the even share, floored at two.
    #
    # Strictly a TIE-BREAK among already-qualified patches: nothing
    # unqualified is promoted, and if the week genuinely holds one type
    # the cap becomes the limit and the quilt fills in rank order rather
    # than padding with weaker material, because an honest monochrome
    # quilt beats a decorative one built from patches that did not earn
    # a tile.
    ceiling = max(limit, 0)
    types_available = len({p.get("patch_type") or "" for _, p in kept[:ceiling * 4]})
    per_type_cap = ceiling
    if types_available > 1:
        share = -(-ceiling // types_available)          # ceil, no float
        per_type_cap = max(MIN_TILES_PER_TYPE, share)

    chosen: List[Tuple[float, Dict[str, Any]]] = []
    deferred: List[Tuple[float, Dict[str, Any]]] = []
    per_type: Dict[str, int] = {}
    for score, patch in kept:
        if len(chosen) >= ceiling:
            break
        ptype = patch.get("patch_type") or ""
        if per_type.get(ptype, 0) >= per_type_cap:
            deferred.append((score, patch))
            continue
        per_type[ptype] = per_type.get(ptype, 0) + 1
        chosen.append((score, patch))
    # A week whose variety ran out still fills, in rank order, rather
    # than showing four tiles because no fifth type existed.
    for pair in deferred:
        if len(chosen) >= ceiling:
            break
        chosen.append(pair)

    plan = layout(len(chosen))
    weights = assign_weights(len(chosen))

    return {
        "patches": [
            {
                "patch_id": patch.get("patch_id"),
                "patch_type": patch.get("patch_type"),
                "fact": _text(patch),
                # NULL IS A REAL STATE, not a missing value to paper
                # over. Section 6.3's rules are enforced by refusal
                # rather than repair, because every repair available is
                # a truncation and that is the exact thing 6.3 forbids,
                # so a patch whose written line broke a rule ships with
                # no headline and the client falls back to the fact.
                # Patches stored before the lane existed are the same
                # state until the backfill reaches them.
                "headline": _value(patch).get("headline") or None,
                "weight": weight,
                "span": span,
                "height": height,
                "source_meeting_id": patch.get("origin_id"),
                "occurred_at": patch.get("created_at"),
                "_salience": score,
            }
            for (score, patch), weight, span, height in zip(
                chosen, weights, plan["spans"], plan["heights"])
        ],
        "row_pairs": row_pairs(len(chosen)),
        "dropped": dropped,
    }

# Section 6.4: a stitch label is at most 24 characters and "reads as a
# thing, not a sentence" -- "$3M ARR goal", "Zero retention constraint".
STITCH_LABEL_MAX = 24

# Words a label must not end on. Cutting mid-phrase is unavoidable
# without a model; ending on a conjunction is not, and it is the
# difference between a short label and a broken one.
_DANGLING = frozenset({
    "and", "or", "of", "the", "a", "an", "to", "for", "with", "in", "on",
    "at", "by", "from", "as", "that", "which", "is", "are", "was", "were",
})

# Where a fact stops being a thing and starts being a sentence about it.
# A colon, a dash or a comma almost always marks that boundary in the
# extraction's own phrasing: "Camino Caseworks business plan documenting
# market opportunity..." wants to stop at "documenting".
# A BARE HYPHEN IS NOT A BOUNDARY. An early version included one and
# turned "60-67% small firms" into "Target market of 60", destroying the
# figure that both 6.3 and 6.4 say to keep. Only an em dash or a SPACED
# hyphen separates clauses; an unspaced one is inside a number or a
# compound word.
_LABEL_BREAK = re.compile(
    r"\s*[:;,()\u2014]\s*"
    r"|\s+-\s+"
    r"|\s+(?:that|which|documenting|covering|including|with|for|to)\s+",
    re.I,
)


def stitch_label(text: Optional[str]) -> str:
    """A short label for a linked patch, derived FROM that patch.

    Never invented, per section 6.4: the label has to be derived from
    the thing it points at, and every link must resolve to a patch the
    user can open, so this is a display string beside an id rather than
    a replacement for one.

    HONEST LIMIT. Section 6.3 says of headlines "no trailing ellipsis,
    rewrite rather than truncate", and the same instinct applies here.
    A rule cannot rewrite. So this CUTS AT A CLAUSE BOUNDARY rather than
    at a character count wherever it can, which yields a phrase instead
    of a fragment, and falls back to whole words with no ellipsis when
    it cannot. A model would do better and this is the deterministic
    floor, the same division as headlines.
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    head = _LABEL_BREAK.split(raw, maxsplit=1)[0].strip(" .")
    if head and len(head) <= STITCH_LABEL_MAX:
        return head
    # No usable boundary: keep whole words, and no ellipsis, because a
    # trailing "..." in a pill reads as a broken string rather than as a
    # deliberately short label.
    out: List[str] = []
    for word in raw.split():
        if len(" ".join(out + [word])) > STITCH_LABEL_MAX:
            break
        out.append(word)
    # A label ending on "and" or "of" reads as a broken string rather
    # than a short one, which is the same objection as the ellipsis.
    while out and out[-1].lower().strip(",;:") in _DANGLING:
        out.pop()
    return " ".join(out).strip(" .,;:") or raw[:STITCH_LABEL_MAX].strip()

