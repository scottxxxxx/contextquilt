"""Second opinion on every behavior observation, made with contrast.

The behavior lane's output is typed by the model that wrote it, and that
model was asked ONE question: how did people conduct themselves. Asked
that, it answers in that shape, and a promise, a decision or a position
comes back dressed as conduct. Measured 2026-09-01 by a Sonnet judge
shown the manifest's own definitions: 59% of 120 stored behaviors were
something else (commitments and decisions above all). No pattern rule
reaches that, and the prompt already says every word of it; both models
ignore it identically, which is what put the pattern rules in code the
day before.

So this is the semantic half of the same fence, and it is a model
because only a model can read semantics. Three things make it safe to
run one here:

CONTRAST. Doc 19.8: a model describes the SHAPE of what it is given, so
a yes/no question about one type produces yeses. The classifier is shown
EVERY type the manifest declares, with the manifest's own description,
and asked which one fits. The right answer is available, so a wrong one
has to be chosen over it rather than defaulted into.

ONE WRITER. A verdict of commitment, decision, event or anything else
the main extraction owns DROPS the row rather than rerouting it. The
main extraction resolves deadlines, stamps owed_to, scopes to a project
and gates on owners; a commitment minted here would carry none of that
and would land in somebody's they_owe ledger as an obligation, or as a
duplicate of the one the main call already stored. A stated preference
is the one exception, converted with `held_by` exactly as the sanitizer
converts it, because the manifest built that edge for this.

FAIL OPEN. A judge failure, a malformed answer, an unknown type or a
missing item all resolve to KEEP. This call must never lose a memory the
lane would otherwise have stored; it may only decline to store one it
was sure about. Kill switch CQ_BEHAVIOR_CLASSIFIER_ENABLED; model
override CQ_BEHAVIOR_CLASSIFIER_MODEL, else the app's own client.

The module is the pure part: prompt, content, parse, apply. The call
and the log live in the worker beside the lane, and
scripts/backfill_behavior_classify.py reuses the same functions on
stored rows so history and the live path are judged by one instrument.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional, Sequence

KEEP_TYPE = "behavior"

# The one type a misfiled behavior is CONVERTED to rather than dropped.
# See `extraction_schema.convert_to_preference` for the mechanics and
# the reason trait is not on this list (it is self_only: a character
# claim about a person who never consented is refused, not routed).
CONVERTIBLE_TYPES = frozenset({"preference"})

# Entity-shaped types are not facts a behavior could be misfiled as, and
# offering them only spends the model's attention.
EXCLUDED_TYPES = frozenset({"person", "org", "project", "identity"})

# A lane call emits at most 12 (behavior_extraction.MAX_OBSERVATIONS);
# the backfill batches at this size.
MAX_ITEMS = 24

ENABLED_ENV = "CQ_BEHAVIOR_CLASSIFIER_ENABLED"
MODEL_ENV = "CQ_BEHAVIOR_CLASSIFIER_MODEL"


def enabled() -> bool:
    return os.getenv(ENABLED_ENV, "true").strip().lower() not in ("0", "false", "no")


def model_override() -> Optional[str]:
    return os.getenv(MODEL_ENV) or None


def classifier_types(manifest: Optional[dict]) -> List[str]:
    """`behavior` first, then every other declared type worth contrasting."""
    names: List[str] = [KEEP_TYPE]
    for t in (manifest or {}).get("patch_types", []) or []:
        if not isinstance(t, dict):
            continue
        n = t.get("domain_type")
        if not isinstance(n, str) or n in names or n in EXCLUDED_TYPES:
            continue
        names.append(n)
    return names


def _definitions(manifest: Optional[dict], names: Sequence[str]) -> str:
    by = {}
    for t in (manifest or {}).get("patch_types", []) or []:
        if isinstance(t, dict) and isinstance(t.get("domain_type"), str):
            by[t["domain_type"]] = t
    lines = []
    for n in names:
        t = by.get(n) or {}
        desc = " ".join(str(t.get("description") or "").split())
        lines.append(f"{n}: {desc[:300]}" if desc else n)
    return "\n".join(lines)


def build_classifier_system(manifest: Optional[dict]) -> str:
    """The judge prompt, with the manifest's own definitions in it.

    The output shape is stated in the prompt because the Anthropic client
    accepts a json_schema for interface parity and never puts it on the
    wire; an unstated shape is an unemitted shape (doc 19.3, and the
    edge bug of 2026-09-01).
    """
    names = classifier_types(manifest)
    return (
        "You are the type check on the behavioral-observation stage of "
        "ContextQuilt, a persistent memory system. That stage records how "
        "named people conducted themselves in one meeting, and it drifts: "
        "asked only about conduct, it also records promises, decisions, "
        "events and positions dressed as conduct. You are shown its output "
        "for one meeting, each item already typed as a behavior, and you "
        "say which type each item actually is.\n\n"
        "A behavior is one thing the named person DID in the room, a "
        "moment a reader watching the meeting could have seen: what they "
        "asked for before agreeing, how they answered a question, what "
        "moved their position. If the item says what the person agreed, "
        "offered or was asked to DO LATER, it is a commitment. If it "
        "records a call that was made and now holds, it is a decision. If "
        "it states what the person prefers, values or leans toward, it is "
        "a preference, even when they said it forcefully. If it is about a "
        "system, a product, a market or the world rather than about the "
        "person's conduct, it is a takeaway or an event. Type each item on "
        "its own merits: it is normal for every item in a batch to be a "
        "behavior, and normal for none to be.\n\n"
        "Choose exactly one type per item from this list, using these "
        "definitions and no others:\n"
        f"{_definitions(manifest, names)}\n\n"
        "NEVER use a dash of any kind as punctuation in anything you "
        "write.\n\n"
        "Respond with EXACTLY this raw JSON shape and nothing else, one "
        "entry per item, each item number used exactly once:\n"
        '{"items": [{"item": 0, "type": "<one type name from the list>"}]}'
    )


def build_classifier_content(patches: Sequence[dict]) -> str:
    """Numbered items. The number is the join key back to the patch, so
    a duplicated or invented id cannot land a verdict on the wrong row."""
    blocks: List[str] = []
    for i, p in enumerate(patches):
        value = p.get("value") if isinstance(p, dict) else None
        value = value if isinstance(value, dict) else {}
        text = " ".join(str(value.get("text") or "").split())[:400]
        owner = " ".join(str(value.get("owner") or "").split())[:80] or "(none)"
        blocks.append(f"ITEM {i}:\nowner: {owner}\nfact: {text}")
    return "\n\n".join(blocks)


def parse_classifier_verdicts(
    content: Any, n_items: int, allowed: Iterable[str],
) -> List[Optional[str]]:
    """One verdict per item, or None where the judge said nothing usable.

    None means KEEP. Anything malformed, out of range, repeated, or not a
    type the manifest declares resolves to None, so a bad answer can only
    fail to drop a row, never drop the wrong one.
    """
    allowed_set = {a for a in allowed if isinstance(a, str)}
    verdicts: List[Optional[str]] = [None] * n_items
    if not isinstance(content, dict):
        return verdicts
    seen: set = set()
    for entry in content.get("items") or []:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("item")
        t = entry.get("type")
        if isinstance(idx, bool) or not isinstance(idx, int):
            continue
        if not (0 <= idx < n_items) or idx in seen:
            continue
        if not isinstance(t, str):
            continue
        t = t.strip().lower()
        if t not in allowed_set:
            continue
        seen.add(idx)
        verdicts[idx] = t
    return verdicts


def apply_classifier_verdicts(
    patches: Sequence[dict], verdicts: Sequence[Optional[str]],
) -> Dict[str, list]:
    """Split the lane's patches by verdict.

    Returns {"kept": [...], "retyped": [...], "dropped": [...]} where
    `kept` and `retyped` are the patches to store (retyped ones already
    converted) and `dropped` is an audit list of {text, owner, verdict}.
    Making the drop audible is the point of the list: the client cannot
    build this half, so the count and the texts go in the log.
    """
    from contextquilt.services.extraction_schema import convert_to_preference

    kept: List[dict] = []
    retyped: List[dict] = []
    dropped: List[dict] = []
    for i, patch in enumerate(patches):
        verdict = verdicts[i] if i < len(verdicts) else None
        if verdict is None or verdict == KEEP_TYPE:
            kept.append(patch)
            continue
        value = patch.get("value") if isinstance(patch.get("value"), dict) else {}
        if verdict in CONVERTIBLE_TYPES:
            convert_to_preference(patch)
            retyped.append(patch)
            continue
        dropped.append({
            "text": str(value.get("text") or "")[:120],
            "owner": value.get("owner"),
            "verdict": verdict,
        })
    return {"kept": kept, "retyped": retyped, "dropped": dropped}
