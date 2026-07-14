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
        rules.append({
            "from_types": from_types,
            "produce_type": produce_type,
            "min_patches": min_patches,
            "guidance": raw.get("guidance") if isinstance(raw.get("guidance"), str) else None,
        })
    return rules


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
