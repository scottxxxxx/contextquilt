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
DROP_NO_HEADLINE = "no_headline_written"
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


def why_not_a_tile(patch: Dict[str, Any],
                   require_headline: bool = True) -> Optional[str]:
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

    # A TILE WITHOUT A HEADLINE IS NOT A TILE, and this rule settles a
    # disagreement rather than expressing a preference.
    #
    # Section 6.3 refuses an invalid headline rather than repairing it,
    # because every repair available is a truncation. That rule was
    # about the WRITER. It left the reader's case unstated, and CQ and
    # ShoulderSurf then filled the gap in opposite directions: CQ said
    # render `fact`, SS's renderer skipped the patch. Both readings were
    # defensible and only one could be right.
    #
    # SS's objection is the one that decides it. `fact` is unbounded and
    # a tile is stamp-sized, so rendering it makes the RENDERER cut the
    # sentence: the same forbidden truncation, arriving through a
    # different door. But skipping client-side is wrong too, because
    # then `total_available` promises tiles that never appear and the
    # holes are invisible from here.
    #
    # So neither side decides it at render time. A patch that cannot be
    # shown as a tile is not SELECTED as one, which makes the contract a
    # sentence long: every tile the home digest serves has a headline.
    # It also puts the loss in `dropped`, where the count is visible and
    # creates pressure to improve the writer, instead of silently
    # thinning somebody's quilt.
    #
    # The seam route is deliberately NOT gated: it is one meeting in
    # capture order with no tiling and no size constraint, so a fact
    # there is the record rather than a broken tile.
    # `require_headline=False` IS FOR THE WRITER, and without it the
    # writer cannot run at all. The headline lane selects patches that
    # have NO headline and then asks this function whether each one
    # could earn a tile. With the gate on, every candidate answers
    # `no_headline_written` by construction, the lane finds nothing to
    # do, and it writes zero headlines forever while looking healthy.
    #
    # That shipped for one commit and CI caught it, in the DB test that
    # EXECUTES the fetch rather than reading it. Nothing in the unit
    # suite could have: they all supply a headline in the fixture,
    # because a tile needs one.
    #
    # So the reader asks "can this be shown", and the writer asks "could
    # this be shown if I gave it a line". One function, one copy of
    # every other rule.
    if require_headline and not _value(patch).get("headline"):
        return DROP_NO_HEADLINE
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
PROTOTYPE_MAX = max(LAYOUTS)

# Scott raised the ceiling from 6 to 60 with paging (2026-08-31), after
# measuring that a real week holds 322 eligible tiles for him and 125
# for the next busiest user, and that serving more costs NO model spend:
# there is no LLM call on the read path, and headlines are written once
# at ingest. The costs are payload (713 bytes a tile) and this table.
MAX_TILES = 60

# ROW TEMPLATES FOR A QUILT LONGER THAN THE PROTOTYPE SPECIFIES.
#
# 1 through 6 stay EXACTLY as the design fixes them and are not
# generated: the prototype is authoritative about what the screen looks
# like, and a formula that happened to reproduce those six would still
# be a second source of truth about them. Upstream prompt caching also
# depends on recall output being byte-stable, and the six-tile shape is
# what ships today.
#
# Above six, the rows repeat this cycle. Each template sums to exactly 6
# columns, which is the invariant the whole grid rests on, and the cycle
# is six rows long so the same row shape never lands three times
# running. That is section 6.1's rhythm argument applied down the scroll
# rather than across one screen.
ROW_CYCLE: List[Tuple[Tuple[int, ...], int]] = [
    ((3, 3), 118),
    ((2, 4), 96),
    ((2, 2, 2), 104),
    ((4, 2), 96),
    ((3, 3), 118),
    ((2, 4), 104),
]

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
    if count <= PROTOTYPE_MAX:
        return LAYOUTS[count]

    spans: List[int] = []
    heights: List[int] = []
    rows: List[Tuple[int, ...]] = []
    placed, step = 0, 0
    count = min(count, MAX_TILES)

    # Stop while a remainder of 1 to 3 is left, because those are
    # exactly the prototype's own single-row layouts and reusing them
    # means the last row of a long quilt is a shape the design already
    # approved rather than one this loop invented.
    while count - placed > 3:
        template, height = ROW_CYCLE[step % len(ROW_CYCLE)]
        if count - placed < len(template):
            break
        rows.append(tuple(range(placed, placed + len(template))))
        spans.extend(template)
        heights.extend([height] * len(template))
        placed += len(template)
        step += 1

    remainder = count - placed
    if remainder:
        tail = LAYOUTS[remainder]
        rows.append(tuple(range(placed, placed + remainder)))
        spans.extend(tail["spans"])
        heights.extend(tail["heights"])

    return {"spans": spans, "heights": heights, "rows": rows}


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



def _order_with_rhythm(
    kept: List[Tuple[float, Dict[str, Any]]], page: int,
) -> List[Tuple[float, Dict[str, Any]]]:
    """Rank order, rearranged so no PAGE is dominated by one type.

    Section 6.1 caps weight-3 tiles at two "so the quilt has rhythm",
    which establishes visual rhythm as a legitimate selection concern.
    Type drives the fabric hue, so the same argument applies to colour:
    an unconstrained ranking on a commitment-heavy week returns five
    commitments and the screen is one purple block. Measured on real
    data, the top six were four commitments and two blockers.

    THE CAP IS PER PAGE, not per quilt, and that is what makes it work
    at 60 tiles as well as 6. A cap on the whole selection would be met
    trivially by a long list and do nothing; a cap on each page of what
    the user actually sees at once keeps the mix all the way down the
    scroll.

    THE CAP IS ALSO DERIVED, NOT FIXED, and the first version got that
    wrong. A flat two deferred four commitments on a week holding only
    two types, then backfilled two of them anyway, so the cap had done
    nothing but shuffle. A cap the material cannot meet is not a cap.
    It is the even share, floored at two.

    Nothing is ever dropped or promoted: every patch that ranked appears
    exactly once, in an order that is a permutation of rank. A week that
    genuinely holds one type comes back in pure rank order rather than
    padded, because an honest monochrome quilt beats a decorative one.
    """
    pool = list(kept)
    out: List[Tuple[float, Dict[str, Any]]] = []
    while pool:
        # THE WHOLE REMAINING POOL, not a window off the front. A
        # window was the first version and it failed exactly where it
        # was needed: on a commitment-heavy week the first 24 ranked
        # patches are ALL commitments, so it counted one type, set the
        # cap to the page size, and returned six commitments. The cap
        # was computed from the very concentration it exists to break.
        types_here = len({p.get("patch_type") or "" for _, p in pool})
        cap = page if types_here <= 1 else max(
            MIN_TILES_PER_TYPE, -(-page // types_here))

        taken: List[int] = []
        per_type: Dict[str, int] = {}
        for idx, (_, patch) in enumerate(pool):
            if len(taken) >= page:
                break
            ptype = patch.get("patch_type") or ""
            if per_type.get(ptype, 0) >= cap:
                continue
            per_type[ptype] = per_type.get(ptype, 0) + 1
            taken.append(idx)
        # Variety ran out before the page filled: take the next in rank
        # rather than showing a short page.
        if len(taken) < page:
            for idx in range(len(pool)):
                if len(taken) >= page:
                    break
                if idx not in taken:
                    taken.append(idx)
            taken.sort()
        for idx in taken:
            out.append(pool[idx])
        pool = [pair for i, pair in enumerate(pool) if i not in set(taken)]
    return out


def _arrange_for_contrast(
    chosen: List[Tuple[float, Dict[str, Any]]],
) -> List[Tuple[float, Dict[str, Any]]]:
    """Order one page so touching tiles differ in type, rank-preferring.

    Counts alone do not make a quilt. A page can be well mixed by the
    numbers and still read as blocks, because what the eye sees is
    NEIGHBOURS, not totals. Rows are runs of consecutive positions, so
    breaking runs in this flat order breaks them in every row.

    Greedy and rank-preferring: at each position take the
    highest-ranked remaining tile whose type differs from the one just
    placed, and fall back to the plain highest-ranked when every
    remaining tile matches. A week of one type therefore comes out in
    exact rank order rather than being shuffled for the sake of it.

    WHY THIS IS FREE, and it is the reason arrangement can be decided
    separately from priority at all: priority decides WHICH patches are
    on the page, and this only decides where they sit within it. The
    prototype's own span pattern is [3, 3, 2, 4, 2, 4], so the largest
    tile is in position four and size was never monotonic in rank. It
    is a decorative arrangement, which is exactly what makes it safe to
    permute.

    An earlier version swapped only tiles of EQUAL span, to guarantee
    nothing changed size. It could not fix the common case: the six
    tile layout has just two span-3 slots, so two commitments landing
    there had no eligible partner anywhere on the page and the run
    survived.

    Deterministic, because recall output must stay byte-stable within a
    UTC day for upstream prompt caching.
    """
    # Grouped by type, each group still in rank order, so the tile a
    # type contributes is always its best remaining one.
    groups: Dict[str, List[Tuple[float, Dict[str, Any]]]] = {}
    for pair in chosen:
        groups.setdefault(pair[1].get("patch_type") or "", []).append(pair)

    out: List[Tuple[float, Dict[str, Any]]] = []
    previous = None
    while any(groups.values()):
        # THE MOST PLENTIFUL TYPE FIRST, not simply a different one.
        # Taking any different type was the previous version and it
        # produced two blocks at 24 tiles: it alternated the two
        # commonest types until they ran out, then alternated the next
        # two. Spending down the largest group first is what spreads
        # every type across the whole page, and it is the same greedy
        # that solves "rearrange so no two neighbours match".
        options = [k for k, v in groups.items() if v and k != previous]
        if not options:
            options = [k for k, v in groups.items() if v]
        # Ties on count are broken by SALIENCE, not alphabetically, and
        # that is not a detail. Sorting the name was the first version
        # and on real data it produced a perfect stripe: with fifteen
        # types each contributing the same couple of tiles, every count
        # tied and the quilt cycled blocker, commitment, constraint,
        # decision, deliverable, event in alphabetical order forever.
        # Regular, and the opposite of a quilt.
        #
        # Ranking the group by its best remaining tile makes the pattern
        # follow the WEEK rather than the alphabet, so it is irregular
        # because the data is, and it puts the more consequential memory
        # earlier whenever the mix allows. Patch id last, so equal
        # scores can never reorder between two calls.
        options.sort(key=lambda k: (-len(groups[k]), -groups[k][0][0],
                                    str(groups[k][0][1].get("patch_id") or "")))
        pick = options[0]
        out.append(groups[pick].pop(0))
        previous = pick
    return out


def build_digest(
    candidates: Iterable[Dict[str, Any]],
    limit: int = 6,
    today: Optional[date] = None,
    edge_counts: Optional[Dict[str, int]] = None,
    offset: int = 0,
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

    # SELECTION AND ARRANGEMENT ARE TWO JOBS, and doing them in one
    # pass is why the first version rendered as a block of one colour.
    #
    #   WHICH patches  -> priority. Rank decides, always.
    #   WHERE they sit -> the quilt. Position decides nothing about
    #                     whether a patch is shown, only where.
    #
    # Scott asked for both on 2026-08-31: "prioritize them somehow, but
    # show a mix so it is actually reminiscent of a quilt". Ranking
    # alone gives a correct list that looks like a spreadsheet; mixing
    # alone gives a pretty screen that buries the thing that mattered.
    # Clamped HERE rather than trusting the route, because the route is
    # not the only caller and the failure is silent: `zip` below would
    # truncate the tiles to the layout's length while `row_pairs` still
    # described the longer list, so the grid would reference positions
    # that were never served.
    limit = min(max(limit, 0), MAX_TILES)
    ordered = _order_with_rhythm(kept, max(limit, 1))
    total = len(ordered)
    start = max(offset, 0)
    chosen = ordered[start:start + max(limit, 0)]
    plan = layout(len(chosen))
    weights = assign_weights(len(chosen))
    chosen = _arrange_for_contrast(chosen)

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
            }
            # `score` is deliberately NOT in the dict above. It shipped
            # as `_salience` for one evening because an internal ranking
            # number was convenient to eyeball, and GP caught that two
            # teams had agreed it would stay internal while it was on the
            # wire. An underscore is a convention, not a boundary: once a
            # field travels, somebody eventually builds on it, and the
            # ranking is the part of this service most likely to change.
            # The `dropped` map and the worker logs answer the questions
            # it was there for.
            for (score, patch), weight, span, height in zip(
                chosen, weights, plan["spans"], plan["heights"])
        ],
        "row_pairs": row_pairs(len(chosen)),
        "dropped": dropped,
        # Paging, so a client never has to infer whether there is more.
        # `total_available` counts what EARNED a tile, after pruning, so
        # it is the honest denominator for "showing N of M" rather than
        # a raw candidate count that includes rows the quilt can never
        # show.
        "total_available": total,
        "offset": start,
        "has_more": start + len(chosen) < total,
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

