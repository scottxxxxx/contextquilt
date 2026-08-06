"""
Counterparty judge: who is an existing action item owed TO?

`owed_to` (manifest v9) gives extraction a place to record the person a
commitment is owed to, but it only fills forward. Everything already in
the quilt was extracted under a vocabulary that had no counterparty, so
without a backfill the People ledger reads "nothing you owe" for a user
whose entire history of obligations predates the label.

This module is the pure part, in the same shape as `semantic_dedup`: the
prompt, the output contract, and the verdict parser. The DB flow lives in
`scripts/backfill_owed_to.py`.

Two design choices worth keeping:

**The candidate list is closed.** The judge is given the exact set of
people CQ already knows for this user and told to answer with one of them
or with nothing. It cannot invent a counterparty, and a backfill has no
business creating person patches, so a name outside the list is discarded
rather than resolved.

**Nothing is the default answer.** Most action items have no stated
recipient, and a wrong counterparty is worse than a missing one: it
renders as an obligation the user does not have, to a person they may not
owe. The prompt says so explicitly and the parser drops anything it
cannot map cleanly.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

# One batched call handles this comfortably; the population it runs
# against is the user's own OPEN items, which is tens, not thousands.
MAX_JUDGE_ITEMS = 40


COUNTERPARTY_JUDGE_SYSTEM = """You identify who an action item is owed TO.

Each numbered ITEM is something the user themselves has to do. You are given a closed list of PEOPLE the user knows. For each item, name the ONE person from that list who is waiting to receive it, or null when nobody in the list clearly is.

Answer with a person ONLY when the item text itself names them as the recipient:
- "Send Lockridge the revised routing diagram" is owed to Lockridge.
- "Get the IP address over to Denby" is owed to Denby.
- "Introduce her to Marcus for the vendor eval" is owed to Marcus.

Answer null in every other case, including all of these:
- The item names nobody ("Finish the migration plan").
- A person is mentioned but is not the recipient ("Review Lockridge's schema draft", "Update the deck Marcus sent").
- The recipient is a group, a team, a company, or somebody not on the list.
- You are weighing two candidates and neither is clearly the one.

Null is the right answer most of the time. A missing counterparty leaves the item where it already is; a wrong one tells the user they owe something to somebody they do not.

Use the person's name EXACTLY as it appears in the PEOPLE list.

Respond with ONLY a JSON object, no prose, no markdown fences, in exactly this shape:
{"verdicts": [{"item": 0, "owed_to": "Lockridge Chen"}, {"item": 1, "owed_to": null}]}
with one entry per item, using each item's number exactly once."""


COUNTERPARTY_JUDGE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdicts"],
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["item", "owed_to"],
                "properties": {
                    "item": {"type": "integer"},
                    "owed_to": {"type": ["string", "null"]},
                },
            },
        },
    },
}


def build_counterparty_content(
    items: Sequence[str], people: Sequence[str]
) -> str:
    """Render the closed people list and the numbered items.

    `items` are the action-item texts, `people` the exact surface forms
    the judge is allowed to answer with.
    """
    people_block = "\n".join(f"- {p}" for p in people)
    item_block = "\n".join(
        f"ITEM {i}: {(t or '').strip().replace(chr(10), ' ')[:300]}"
        for i, t in enumerate(items)
    )
    return f"PEOPLE:\n{people_block}\n\nITEMS:\n{item_block}"


def parse_counterparty_verdicts(
    content: Any, n_items: int, people: Sequence[str]
) -> List[str | None]:
    """Map the judge's output to a per-item name-or-None list.

    Defensive in the same direction as the prompt: anything malformed,
    out of range, or naming somebody outside the closed candidate list
    resolves to None. Matching is case-insensitive because the model
    re-cases names, but the RETURNED value is always the exact surface
    form from `people`, so the caller can look it up without a second
    normalisation step.
    """
    verdicts: List[str | None] = [None] * n_items
    if not isinstance(content, dict):
        return verdicts
    by_lower = {(p or "").strip().lower(): p for p in people if (p or "").strip()}
    for v in content.get("verdicts") or []:
        if not isinstance(v, dict):
            continue
        idx = v.get("item")
        name = v.get("owed_to")
        if not isinstance(idx, int) or not 0 <= idx < n_items:
            continue
        if not isinstance(name, str):
            continue
        match = by_lower.get(name.strip().lower())
        if match is not None:
            verdicts[idx] = match
    return verdicts
