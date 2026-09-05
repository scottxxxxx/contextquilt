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
CQ_BEHAVIOR_CLASSIFIER_MODEL, default Sonnet (see DEFAULT_MODEL).

MEASURED, not claimed (2026-09-01, Scott's rows, two 120-row samples,
one hand reader): with this prompt Sonnet keeps 103 and 104 of 120,
and every drop in both samples read as correct or a toss-up, so the
loss side is clean. The kept side still carries roughly one status
relay in twelve ("Explained that version 2.2 launched two weeks ago"),
which puts agreement with the hand read near 92%, not the 95% asked
for. A stricter wording reached about 95% on the first sample and paid
for it with three real drops in 120, and the difference between the
two is inside what one reader can resolve on ambiguous rows. The
lenient side was chosen on purpose.

The module is the pure part: prompt, content, parse, apply. The call
and the log live in the worker beside the lane, and
scripts/backfill_behavior_classify.py reuses the same functions on
stored rows so history and the live path are judged by one instrument.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional, Sequence

KEEP_TYPE = "moment"

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

# The stakes gate (2026-09-04). Read all 46 moments Scott's account kept
# in one week after the choice rule: 24 were choices a reader would want
# to know about, 12 were the mechanics of holding a meeting (who is
# joining, upload the file, check whether Tripp is on), 7 were narration
# that slipped, 3 were noise. The choice rule cannot touch the 12,
# because delegating a task IS a choice. What they lack is stakes: a
# memory of the person made of them would say nothing. So the judge is
# asked a fourth question on every item it keeps as a moment, and a
# routine answer drops the row through the same path a commitment
# verdict takes. `routine` is a pseudo-verdict: parse produces it only
# from the stakes field, never from `type`, so a model that writes
# "routine" as a type is ignored (kept), same as any unknown type.
STAKES_ENV = "CQ_MOMENT_STAKES_GATE_ENABLED"
ROUTINE_VERDICT = "routine"
STAKES_VALUES = frozenset({"durable", ROUTINE_VERDICT})

# Sonnet, not the app's Haiku client, and measured rather than assumed
# (2026-09-01, 120 stored rows, same prompt, same rows): Haiku dropped
# 26 rows Sonnet kept and every one of the 26 that was read by hand was
# conduct ("Instructed Joy to look at the deployment", "Deferred the
# decision until Monday", "Confirmed that 624 is still good"), filed as
# the other person's commitment or as an event. Sonnet's misses ran the
# other way, keeping a status relay now and then, which is the failure
# that costs nothing. A wrong drop here is a memory nobody can
# re-observe, because the transcript is not retained. At roughly seven
# rows a meeting the difference is under half a cent.
DEFAULT_MODEL = "claude-sonnet-4-6"


def enabled() -> bool:
    return os.getenv(ENABLED_ENV, "true").strip().lower() not in ("0", "false", "no")


def model_override() -> Optional[str]:
    return os.getenv(MODEL_ENV) or DEFAULT_MODEL


def stakes_gate_enabled() -> bool:
    return os.getenv(STAKES_ENV, "true").strip().lower() not in ("0", "false", "no")


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
        "person's conduct, it is a takeaway or an event. Two tests that "
        "settle most items. First, the row is about its OWNER: when the "
        "owner asked, instructed, assigned or directed somebody ELSE to do "
        "a thing, the row records the owner's conduct and is a behavior; "
        "the task belongs to the other person and is not this row's type. "
        "Second, remove the owner's name and ask whether what is left is "
        "still true: a market size, a date, a method, a rule of the world "
        "stays true without them and is a takeaway or an event however it "
        "is phrased, while conduct disappears with the person. A pure "
        "status relay fails that test too: what has launched, what is "
        "pending, what was raised last week and where a document currently "
        "sits are events when the row says nothing about how the owner "
        "handled it. But declining, confirming, requesting, deferring and "
        "pushing back are conduct even when a status is mentioned in the "
        "same breath, and those stay behavior. Third, and this one "
        "decides the items the first two let through: a behavior is a "
        "CHOICE, something the person did when something else was "
        "available to them. Asking for the breakdown before agreeing is a "
        "choice, because agreeing was available. Moving off the date once "
        "the numbers were up is a choice, because holding was available. "
        "Saying the critique was about the design and not the person is a "
        "choice, because just delivering it was available. Looking "
        "something up when asked, noticing a bug, being told about a "
        "reassignment and relaying where a task stands are not choices, "
        "they are things that happened while the person was present, and "
        "they are events however active the verb makes them sound. If you "
        "cannot name what the person could have done instead, it is not a "
        "behavior. Type each "
        "item on its own merits: it is normal for every item in a batch to "
        "be a behavior, and normal for none to be.\n\n"
        f"{STAKES_RULE if stakes_gate_enabled() else ''}"
        "Choose exactly one type per item from this list, using these "
        "definitions and no others:\n"
        f"{_definitions(manifest, names)}\n\n"
        "NEVER use a dash of any kind as punctuation in anything you "
        "write.\n\n"
        "Respond with EXACTLY this raw JSON shape and nothing else, one "
        "entry per item, each item number used exactly once:\n"
        + (STAKES_SHAPE if stakes_gate_enabled() else PLAIN_SHAPE)
    )


PLAIN_SHAPE = '{"items": [{"item": 0, "type": "<one type name from the list>"}]}'
STAKES_SHAPE = (
    '{"items": [{"item": 0, "type": "<one type name from the list>", '
    '"stakes": "<durable or routine>"}]}'
)

# The fourth test. Written for the model that copies punctuation, so no
# dash anywhere in it; the no-dash test in tests/unit scans this text.
STAKES_RULE = (
    "Fourth, for every item you keep as a moment, say whether it has "
    "STAKES. An item has stakes when the choice would still tell a reader "
    "something about how this person operates a month from now: what "
    "they asked for before agreeing, a position they took or moved off, "
    "something they declined, withheld or pushed back on, how they "
    "handled a person, a judgment they made with something on the line. "
    "An item is routine when it is the mechanics of holding a meeting or "
    "working together: who is joining, checking whether somebody is on "
    "the call, sharing a screen, where a file lives and who should upload "
    "it, passing a small task to the person who was always going to do "
    "it, a clarifying question any listener would have asked, or a "
    "courtesy. Routine items are real conduct and still not worth "
    "keeping, because a memory of this person made of them would say "
    "nothing. Mark them routine. When you cannot tell, mark durable. The "
    "stakes field is read only on items you typed as a moment; on any "
    "other type write durable and it is ignored.\n\n"
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
        # The stakes gate. Only a kept moment can be routine; the field
        # is ignored on every other type, and anything but the literal
        # "routine" (missing, misspelled, a number) leaves the row a
        # moment, so a bad answer can only fail to drop, never drop.
        if t == KEEP_TYPE and stakes_gate_enabled():
            stakes = entry.get("stakes")
            if isinstance(stakes, str) and stakes.strip().lower() == ROUTINE_VERDICT:
                t = ROUTINE_VERDICT
        verdicts[idx] = t
    return verdicts


def apply_classifier_verdicts(
    patches: Sequence[dict], verdicts: Sequence[Optional[str]],
) -> Dict[str, list]:
    """Split the lane's patches by verdict.

    Returns {"kept": [...], "retyped": [...], "dropped": [...]} where
    `kept` and `retyped` are the patches to store (retyped ones already
    converted) and `dropped` is an audit list of {text, owner, verdict}.
    A routine moment (the stakes gate) drops through the same branch as
    a commitment verdict, with verdict "routine" on its receipt; the
    backfill writes it as archive_detail classified_routine.
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
