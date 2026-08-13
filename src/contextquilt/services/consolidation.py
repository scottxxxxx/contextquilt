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
from typing import Any, Dict, List, Optional

# Bounds — cost and blast-radius caps, not tunables to chase.
MIN_CLUSTER_SIZE_FLOOR = 2
DEFAULT_MIN_PATCHES = 3
MAX_CLUSTERS_PER_USER_PER_CYCLE = 3
MAX_USERS_PER_APP_PER_CYCLE = 20
CLUSTER_WINDOW_DAYS = 180
MAX_SOURCE_TEXTS = 10  # prompt size cap per synthesis call

# The profile pass (design 16a / 12a): person-keyed clustering. The
# receipts gate is the 12a audit's invariant: a claim about a person
# must be supported across at least this many DISTINCT meetings, or it
# is an anecdote wearing a pattern's clothes.
MIN_MEETINGS_FLOOR = 2
DEFAULT_MIN_MEETINGS = 3
CLUSTER_KEYS = {"cue", "person"}
# The 12b lens vocabulary v1. A response naming any other lens is
# declined, never coerced: the model does not get to invent lenses.
#
# The vocabulary size is also the per-person insight ceiling: 16a stacks
# up to one card per lens, and the profile pass stops considering a
# person once every lens carries a stamp (see
# `worker._consolidate_user_people`). Adding a lens here therefore
# reopens every person for one more derivation, which is intended.
PROFILE_LENSES = {"how_they_decide", "what_moves_them"}


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
- The claim is one plain sentence about the person, no hedging prefixes.
- The do line is one short imperative sentence telling the user how to work with this pattern in their next meeting.
- Write in the same language as the observations.
- Never invent specifics (names, dates, numbers) that appear in no observation.
- Decline freely: a wrong profile is worse than none.

Respond with EXACTLY this raw JSON shape and nothing else:
{"skip": <true|false>, "lens": "<how_they_decide|what_moves_them|null>", "text": "<the pattern claim, or empty string when skip is true>", "do": "<the actionable line, or empty string when skip is true>", "reason": "<one short sentence>"}"""


def remaining_lenses(taken: Optional[Any] = None) -> List[str]:
    """The lenses still underived for a person, sorted for stable prompts.

    `taken` is every lens already stamped for this person in ANY status,
    because a suppressed card is a durable no for that lens (see
    `worker._consolidate_user_people`). Values outside the vocabulary
    are ignored rather than trusted: a drifted stamp must not silently
    retire a real lens.
    """
    taken_set = {t for t in (taken or ()) if t in PROFILE_LENSES}
    return sorted(PROFILE_LENSES - taken_set)


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
                t for t in taken_lenses if t in PROFILE_LENSES
            ))
        )
        lines.append(
            "Lenses still open: "
            + (", ".join(open_lenses) if open_lenses else "none, decline")
        )
    lines.append("")
    lines.append("Observations (dated, oldest first):")
    for date_s, text in dated_texts[:MAX_SOURCE_TEXTS]:
        lines.append(f"- [{date_s}] {text}")
    return "\n".join(lines)


def parse_profile_response(content: Any) -> Optional[Dict[str, str]]:
    """{"lens", "text", "do"} or None for skip/refusal/garbage.

    Same acceptance posture as the cue pass, plus the lens whitelist:
    the model does not get to invent lenses, and a claim without an
    actionable line is declined (16a renders both or neither)."""
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
    if lens not in PROFILE_LENSES:
        return None
    text = obj.get("text")
    do = obj.get("do")
    if not isinstance(text, str) or not isinstance(do, str):
        return None
    text = " ".join(text.split())
    do = " ".join(do.split())
    if not (10 <= len(text) <= 500) or not (5 <= len(do) <= 200):
        return None
    return {"lens": lens, "text": text, "do": do}


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
