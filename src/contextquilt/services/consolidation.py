"""
Consolidation — the "sleep" pass (roadmap #5, active enrichment).

Stored patches are inert: six takeaways about the same topic never
become the durable trait a human would have generalized by morning.
The worker's consolidation loop finds clusters of related episode-grade
patches and synthesizes ONE higher-order patch per cluster, with full
provenance back to its sources.

Design decisions (doc 14 has the long form):

- **Clusters form on shared cues.** The associative-retrieval cue index
  doubles as the clustering key: a cluster is (user, app, source type ∈
  rule.from_types, shared cue) with ≥ rule.min_patches active members.
  Deterministic, cheap (one GROUP BY), and language-agnostic.
- **Rules live in manifests, nowhere else.** `consolidation_rules` is a
  top-level manifest key; no rules → no consolidation for that app's
  patches. Shipping this code is therefore inert until an app opts in —
  the same rollout shape the cue index used. Env kill switch
  CQ_CONSOLIDATION_ENABLED on top.
- **Provenance is mandatory.** Derived patches carry
  origin_mode='derived', the source patch ids in source_patch_ids,
  `informs` connections from each source, and value.source_cue. A bad
  generalization is traceable and deletable.
- **One consolidation per (user, app, rule, cue).** The source_cue
  stamp is the idempotency key; the loop never re-synthesizes a cue it
  already consolidated. (Refreshing a stale insight when its cluster
  grows is a deliberate non-goal for v1.)
- **The LLM may decline.** The synthesis prompt asks for skip=true when
  the cluster doesn't actually support one durable statement; parse
  failure or refusal skips the cluster. Never force an insight.

This module holds the pure parts (rule parsing, prompt, response
parsing) so they're locally testable; the loop in worker.py does I/O.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .follow_through import (
    FOLLOW_THROUGH_LENS,
    MIN_JUDGED_ITEMS,
    judge_items,
)
from .insight_cards import CARD_SHAPE_RULES, card_defect

# Bounds — cost and blast-radius caps, not tunables to chase.
MIN_CLUSTER_SIZE_FLOOR = 2
DEFAULT_MIN_PATCHES = 3
MAX_CLUSTERS_PER_USER_PER_CYCLE = 3
MAX_USERS_PER_APP_PER_CYCLE = 20
CLUSTER_WINDOW_DAYS = 180
MAX_SOURCE_TEXTS = 10  # prompt size cap per CUE synthesis call

# The profile pass gets its own, far higher cap. Measured 2026-08-13:
# the largest live person cluster cites 29 sources, source texts average
# 114 characters, and the whole profile prompt runs about 3.6 KB at ten
# sources against 7.7 KB at forty, roughly 900 tokens against 1,900. A
# whole consolidation cycle made two LLM calls that day. So ten was
# never buying anything. It hid most of a person's record to save a few
# hundred tokens, and the fetch was already uncapped, so the pass paid
# to read the whole cluster and then showed the model a tenth of it.
#
# 60 covers every live cluster twice over while still bounding a
# pathological one, and spread_sample stays the trimmer above it, so the
# behavior we get above 60 is the behavior we had at 10 rather than a
# cliff. Raising this is also the experiment that sizes the rest of the
# longitudinal work: if the decline rate moves, the problem was the
# sample; if it does not, the problem is the corpus.
MAX_PROFILE_SOURCE_TEXTS = 60

# The profile pass (design 16a / 12a): person-keyed clustering. The
# receipts gate is the 12a audit's invariant: a claim about a person
# must be supported across at least this many DISTINCT meetings, or it
# is an anecdote wearing a pattern's clothes.
MIN_MEETINGS_FLOOR = 2
DEFAULT_MIN_MEETINGS = 3
CLUSTER_KEYS = {"cue", "person"}
# The 12b lens vocabulary, in two halves that are produced by two
# different kinds of pass.
#
# MODEL_CHOSEN_LENSES are the ones a model reads observations and picks
# between. A response naming anything outside this set is declined, never
# coerced: the model does not get to invent lenses, and it does not get
# to reach for a lens whose verdict is not its to make.
MODEL_CHOSEN_LENSES = {"how_they_decide", "what_moves_them"}
# COMPUTED_LENSES are decided by arithmetic before any call happens. The
# model writes the sentence; it never chooses the lens or the verdict.
# services/follow_through.py has the whole argument for why the third
# lens had to be built this way round.
COMPUTED_LENSES = {FOLLOW_THROUGH_LENS}
# The whole vocabulary: what a person's stack can hold, what a lens stamp
# may say, and what the readiness surface reports on.
PROFILE_LENSES = MODEL_CHOSEN_LENSES | COMPUTED_LENSES


def parse_consolidation_rules(manifest: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extract well-formed consolidation rules from a manifest.

    Malformed entries are dropped silently — the validator rejects them
    at registration; this re-check only guards drifted/legacy rows."""
    if not isinstance(manifest, dict):
        return []
    declared = {
        pt.get("domain_type")
        for pt in manifest.get("patch_types") or []
        if isinstance(pt, dict)
    }
    rules: List[Dict[str, Any]] = []
    for raw in manifest.get("consolidation_rules") or []:
        if not isinstance(raw, dict):
            continue
        from_types = raw.get("from_types")
        produce_type = raw.get("produce_type")
        if not (isinstance(from_types, list) and from_types
                and all(isinstance(t, str) and t in declared for t in from_types)):
            continue
        if not (isinstance(produce_type, str) and produce_type in declared):
            continue
        min_patches = raw.get("min_patches", DEFAULT_MIN_PATCHES)
        if not isinstance(min_patches, int) or min_patches < MIN_CLUSTER_SIZE_FLOOR:
            min_patches = DEFAULT_MIN_PATCHES
        cluster = raw.get("cluster", "cue")
        if cluster not in CLUSTER_KEYS:
            continue
        min_meetings = raw.get("min_meetings", DEFAULT_MIN_MEETINGS)
        if not isinstance(min_meetings, int) or min_meetings < MIN_MEETINGS_FLOOR:
            min_meetings = DEFAULT_MIN_MEETINGS
        rules.append({
            "from_types": from_types,
            "produce_type": produce_type,
            "min_patches": min_patches,
            "cluster": cluster,
            "min_meetings": min_meetings,
            "guidance": raw.get("guidance") if isinstance(raw.get("guidance"), str) else None,
        })
    return rules


def manifest_declares_person_insights(manifest: Optional[Dict[str, Any]]) -> bool:
    """Whether this app's manifest can ever produce a person insight.

    The profile pass only runs for apps that declare a person-clustered
    consolidation rule, so for anyone else the insight stack is not
    empty, it is unavailable. The People `capabilities` block reports
    that difference (doc 16 section 6.4), on the same principle as
    `you_owe`: the capability follows the schema that produces the data,
    not the code that reads it.
    """
    return any(
        rule["cluster"] == "person"
        for rule in parse_consolidation_rules(manifest)
    )


CONSOLIDATION_SYSTEM = """You are the memory-consolidation stage of ContextQuilt, a persistent memory system. You are shown several stored memory observations about ONE topic, all concerning the same person. Your job is what sleep does for human memory: decide whether these observations, taken together, support ONE durable higher-order statement — and write it if so.

Rules:
- The statement must be supported by the PATTERN across observations, not by any single one. If the observations don't genuinely converge, decline.
- Write in the same language as the observations.
- Never invent specifics (names, dates, numbers) that appear in no observation.
- One plain sentence, no hedging prefixes like "It seems".

Respond with EXACTLY this raw JSON shape and nothing else:
{"skip": <true|false>, "text": "<the durable statement, or empty string when skip is true>", "reason": "<one short sentence: why consolidated or why declined>"}"""


def build_synthesis_content(
    cue: str,
    produce_type: str,
    source_texts: List[str],
    guidance: Optional[str] = None,
) -> str:
    """User-content block for one cluster's synthesis call."""
    lines = [
        f"Topic (shared cue): {cue}",
        f"Target statement type: {produce_type}",
    ]
    if guidance:
        lines.append(f"App guidance: {guidance}")
    lines.append("")
    lines.append("Observations:")
    for i, text in enumerate(source_texts[:MAX_SOURCE_TEXTS], 1):
        lines.append(f"{i}. {text}")
    return "\n".join(lines)


PROFILE_SYSTEM = """You are the memory-consolidation stage of ContextQuilt, a persistent memory system. You are shown dated observations about ONE person, gathered across several different meetings. Your job is the profile pass: decide whether these observations, taken together, reveal ONE durable behavioral pattern about this person, and describe it if so.

Rules:
- The pattern must hold ACROSS meetings, not within one. A single meeting, however vivid, is an anecdote; decline it.
- Choose the one lens the evidence actually supports: "how_they_decide" (how this person reaches and keeps decisions) or "what_moves_them" (what kinds of framing or evidence they respond to). If neither fits, decline.
""" + CARD_SHAPE_RULES + """
- No hedging prefixes like "It seems".
- Write in the same language as the observations.
- Never invent specifics (names, dates, numbers) that appear in no observation.
- Decline freely: a wrong profile is worse than none.

Respond with EXACTLY this raw JSON shape and nothing else:
{"skip": <true|false>, "lens": "<how_they_decide|what_moves_them|null>", "text": "<the pattern claim, or empty string when skip is true>", "do": "<the actionable line, or empty string when skip is true>", "reason": "<one short sentence>"}"""


def remaining_lenses(taken: Optional[Any] = None) -> List[str]:
    """The MODEL-CHOSEN lenses still underived for a person, sorted.

    `taken` is every lens already stamped for this person in ANY status,
    because a suppressed card is a durable no for that lens (see
    `worker._consolidate_user_people`). Values outside the vocabulary
    are ignored rather than trusted: a drifted stamp must not silently
    retire a real lens.

    Computed lenses are deliberately absent from both sides of the
    subtraction. They are never offered to the profile call, so a person
    whose only open lens is a computed one has nothing left for that call
    and it returns [] rather than spending one.
    """
    taken_set = {t for t in (taken or ()) if t in MODEL_CHOSEN_LENSES}
    return sorted(MODEL_CHOSEN_LENSES - taken_set)


def spread_sample(items: Sequence[Any], k: int) -> List[Any]:
    """At most k items spread evenly across an ordered sequence.

    The pass used to hand the model `items[:k]`, which for a person with
    39 qualifying sources meant the OLDEST 10 and nothing from the last
    two months: durable behavior only, recent behavior structurally
    invisible. A spread keeps both ends of the window, always includes
    the first and last item, and is deterministic, so a rerun on an
    unchanged corpus builds identical prompt bytes.
    """
    n = len(items)
    if k <= 0:
        return []
    if n <= k:
        return list(items)
    if k == 1:
        return [items[-1]]  # one slot goes to the most recent behavior
    idx = sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})
    return [items[i] for i in idx]


def build_profile_content(
    person_name: str,
    dated_texts: List[tuple],
    guidance: Optional[str] = None,
    taken_lenses: Optional[Any] = None,
) -> str:
    """User-content block for one person cluster's profile call.
    dated_texts is [(iso_date_str, text), ...] in chronological order;
    the dates matter because a pattern is a claim about time.

    `taken_lenses` names the lenses this person already has, so the call
    is not spent re-deriving one that will be refused on the way in. It
    is a hint for cost, never the invariant: the post-check in
    `_synthesize_person_cluster` is what actually holds the line, since
    the model is free to ignore anything in a prompt.
    """
    lines = [f"Person: {person_name}"]
    if guidance:
        lines.append(f"App guidance: {guidance}")
    open_lenses = remaining_lenses(taken_lenses)
    if taken_lenses:
        lines.append(
            "Lenses already recorded for this person (do not choose these "
            "again): " + ", ".join(sorted(
                t for t in taken_lenses if t in MODEL_CHOSEN_LENSES
            ))
        )
        lines.append(
            "Lenses still open: "
            + (", ".join(open_lenses) if open_lenses else "none, decline")
        )
    lines.append("")
    lines.append("Observations (dated, oldest first):")
    # A spread across the window, not the oldest slice of it. The cap is
    # prompt size; which items it drops is a quality decision, and
    # dropping everything recent was the wrong one.
    for date_s, text in spread_sample(dated_texts, MAX_PROFILE_SOURCE_TEXTS):
        lines.append(f"- [{date_s}] {text}")
    return "\n".join(lines)


def parse_profile_response(
    content: Any,
    person_name: Optional[str] = None,
    defects: Optional[List[str]] = None,
) -> Optional[Dict[str, str]]:
    """{"lens", "text", "do"} or None for skip/refusal/garbage.

    Same acceptance posture as the cue pass, plus the lens whitelist:
    the model does not get to invent lenses, and a claim without an
    actionable line is declined (16a renders both or neither).

    The card shape (both ceilings, and the person's own name banned from
    the opening) is enforced here rather than requested in the prompt,
    because a claim the UI cannot render is worse than no claim. Pass
    `defects` to collect the reason: a rejected FORMAT is a different
    event from a model choosing to skip, and only one of them is worth
    waking up for."""
    obj = content
    if isinstance(obj, str):
        m = re.search(r"\{.*\}", obj, re.DOTALL)
        if not m:
            return None
        try:
            obj = json.loads(m.group())
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict):
        return None
    if obj.get("skip") is not False and obj.get("skip") is not None:
        return None
    lens = obj.get("lens")
    # MODEL_CHOSEN, not the whole vocabulary: a computed lens is not on
    # offer here, so naming one is as invalid as inventing one.
    if lens not in MODEL_CHOSEN_LENSES:
        return None
    text = obj.get("text")
    do = obj.get("do")
    if not isinstance(text, str) or not isinstance(do, str):
        return None
    text = " ".join(text.split())
    do = " ".join(do.split())
    defect = card_defect(text, do, person_name)
    if defect:
        if defects is not None:
            defects.append(defect)
        return None
    return {"lens": lens, "text": text, "do": do}


def person_insight_rule(manifest: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The app's person-clustered rule, or None if it declares none.

    The read surface needs the same thresholds the pass runs on, because
    "two more meetings" is only honest if it counts toward the number
    that actually gates the derivation.
    """
    for rule in parse_consolidation_rules(manifest):
        if rule["cluster"] == "person":
            return rule
    return None


# The readiness vocabulary. Open, like `decay_state` and `lens`: a client
# that meets a state it does not know must render NOTHING for that lens,
# never the not-yet copy, because the not-yet copy is a promise.
#
# Only the two `pending_` states invite waiting. `suppressed` and
# `retired` both mean the pass will not produce this card again (the
# durable no ignores status, so any stamp closes the lens), and telling a
# user to keep meeting someone so a claim they permanently rejected can
# come back is the one thing this surface must never do. Clients that do
# not want to reason about the vocabulary can read `more_meetings_help`,
# which answers exactly that question and nothing else.
READINESS_AVAILABLE = "available"
READINESS_SUPPRESSED = "suppressed"
READINESS_RETIRED = "retired"
READINESS_PENDING_EVIDENCE = "pending_evidence"
READINESS_PENDING_PATTERN = "pending_pattern"
READINESS_WAITING_STATES = {READINESS_PENDING_EVIDENCE, READINESS_PENDING_PATTERN}

# The archive cause DELETE /v1/patches stamps. Any other cause on an
# archived insight means the system retired the card, not the user.
USER_SUPPRESSION_CAUSE = "user_delete"


def _lens_state(stamps: List[Mapping[str, Any]], gate_met: bool) -> str:
    for stamp in stamps:
        if (stamp.get("status") or "active") == "active":
            return READINESS_AVAILABLE
    if any(stamp.get("archive_cause") == USER_SUPPRESSION_CAUSE for stamp in stamps):
        return READINESS_SUPPRESSED
    if stamps:
        return READINESS_RETIRED
    return READINESS_PENDING_PATTERN if gate_met else READINESS_PENDING_EVIDENCE


def build_insight_readiness(
    source_rows: Iterable[Mapping[str, Any]],
    stamp_rows: Iterable[Mapping[str, Any]],
    today: date,
    min_patches: int,
    min_meetings: int,
) -> Dict[str, Any]:
    """Per lens: where this person stands, and whether waiting helps.

    Serving this is what lets a client say "two more meetings with Priya"
    instead of "check back later", and it is what stops it saying either
    one about a lens the user already threw away. Both halves matter: the
    numbers make the empty state specific, the state makes it honest.

    `source_rows` are the person's owns-edge items of the rule's types,
    ACTIVE and COMPLETED alike, because the two lens families count
    different things. The model lenses count what their cluster SQL
    counts: active items carrying a meeting. The computed lens counts
    items whose due date has come due, which is mostly items that already
    closed, and closing archives the row.

    Every entry carries every sibling key (doc 17 section 6): a number is
    null only when it is genuinely unknown, never merely inapplicable,
    and every count here is an int, so nothing on this surface can reach
    a strict JSON serializer as NaN or Infinity.
    """
    rows = list(source_rows or ())
    by_lens: Dict[str, List[Mapping[str, Any]]] = {}
    for stamp in stamp_rows or ():
        lens = stamp.get("lens")
        if lens:
            by_lens.setdefault(lens, []).append(stamp)

    # The model-lens gate, counted exactly as the cluster query counts it.
    live = [r for r in rows
            if (r.get("status") or "active") == "active" and r.get("origin_id")]
    model_items = len(live)
    model_meetings = len({str(r["origin_id"]) for r in live})
    # The computed-lens gate, from the same arithmetic the pass runs on.
    computed = judge_items(rows, today)["facts"]

    lenses = []
    for lens in sorted(PROFILE_LENSES):
        if lens in COMPUTED_LENSES:
            items, meetings = computed["judged_items"], computed["meetings"]
            need_items = MIN_JUDGED_ITEMS
        else:
            items, meetings = model_items, model_meetings
            need_items = min_patches
        gate_met = items >= need_items and meetings >= min_meetings
        state = _lens_state(by_lens.get(lens, []), gate_met)
        lenses.append({
            "lens": lens,
            "state": state,
            "more_meetings_help": state in READINESS_WAITING_STATES,
            "items_observed": items,
            "items_required": need_items,
            "items_remaining": max(0, need_items - items),
            "meetings_observed": meetings,
            "meetings_required": min_meetings,
            "meetings_remaining": max(0, min_meetings - meetings),
        })
    return {"lenses": lenses}


def parse_synthesis_response(content: Any) -> Optional[str]:
    """The synthesized statement, or None for skip/refusal/garbage.

    Accepts a dict (structured-output providers) or raw text containing
    the JSON object (the Anthropic client does not enforce json_schema
    on the wire)."""
    obj = content
    if isinstance(obj, str):
        m = re.search(r"\{.*\}", obj, re.DOTALL)
        if not m:
            return None
        try:
            obj = json.loads(m.group())
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict):
        return None
    if obj.get("skip") is not False and obj.get("skip") is not None:
        return None
    text = obj.get("text")
    if not isinstance(text, str):
        return None
    text = " ".join(text.split())
    # A durable statement is one sentence, not an essay and not a stub.
    if not (10 <= len(text) <= 500):
        return None
    return text
