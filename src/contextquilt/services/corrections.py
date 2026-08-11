"""
User corrections from chat (context-flow contract item 9).

A user sees a memory-backed answer, spots a wrong fact, and says so in
chat ("the deadline moved to August", "Robin owns that, not Cindy").
GP detects the intent and sends interaction_type=correction with the
correction text, the project/origin scope, and (when available) the
recall block that was in context. The worker:

1. Builds a candidate set — patches whose text appeared in the passed
   context_block first (those were on the user's screen), then scoped
   recent patches. Capped for the prompt.
2. One LLM call using the proven resolved_commitments pattern: the
   prompt carries candidates WITH patch ids and the model returns the
   contradicted id (or null) plus the corrected fact. Post-hoc text
   matching is never attempted — the model picks from an explicit
   candidate list or declines.
3. Supersede write, all existing vocabulary: the corrected fact is a
   NEW patch with origin_mode='declared' (user-stated facts are the
   first real use of the declared lane), the stale patch is ARCHIVED
   (flows through the quilt delta `deleted` array, devices converge),
   and the two are connected with role 'replaces'.
4. Unmatched corrections still land as declared patches — the user
   explicitly stated a fact, and the standing rule is that a broken
   judge must never lose a memory. Logged unmatched for observability.

This module holds the pure parts (prompt, content builder, response
parsing); the I/O lives in worker.handle_correction.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from contextquilt.services.extraction_schema import PATCH_TYPES, validate_deadline_date

MAX_CANDIDATES = 20
MAX_CORRECTION_CHARS = 2000
# Type for an unmatched correction when the model doesn't name a better
# one — a plain remembered fact.
FALLBACK_PATCH_TYPE = "takeaway"

CORRECTION_SYSTEM = """You are the correction stage of ContextQuilt, a persistent memory system. A user has just told their assistant that something in memory is wrong. You are shown the user's correction and a numbered list of stored memory patches, each with its patch_id. Your job:

1. Decide which stored patch (if any) the correction contradicts. Only pick a patch the correction actually contradicts or updates — topical similarity is not contradiction. If none qualifies, use null.
2. Write the corrected fact as it should now be remembered: one concise statement incorporating the user's correction. Keep the same language the user wrote in. If the correction carries a deadline, resolve it to a calendar date using the current date provided.

Respond with EXACTLY this raw JSON shape and nothing else:
{"corrected_patch_id": "<patch_id copied verbatim from the list, or null>", "corrected_fact": {"text": "<the corrected statement>", "owner": "<responsible person, or null>", "deadline": "<deadline as the user said it, or null>", "deadline_date": "<YYYY-MM-DD or null>", "patch_type": "<only when corrected_patch_id is null: one of the allowed types, else null>"}, "reason": "<one short sentence>"}"""


def build_correction_content(
    correction_text: str,
    candidates: List[Dict[str, Any]],
    today_iso: str,
    scope_label: Optional[str] = None,
    allowed_types: Optional[List[str]] = None,
) -> str:
    """User-content block for the correction call. Candidates are dicts
    with patch_id, patch_type, text (already ordered: in-block first)."""
    lines = [f"Current date: {today_iso}"]
    if scope_label:
        lines.append(f"Project scope: {scope_label}")
    lines.append(f"Allowed types for a new fact: {', '.join(allowed_types or sorted(PATCH_TYPES))}")
    lines.append("")
    lines.append(f"User's correction: {correction_text}")
    lines.append("")
    lines.append("Stored patches (candidates):")
    for i, c in enumerate(candidates[:MAX_CANDIDATES], 1):
        lines.append(f"{i}. [{c['patch_type']}] patch_id={c['patch_id']} :: {c['text']}")
    if not candidates:
        lines.append("(none — memory holds no candidate patches in this scope)")
    return "\n".join(lines)


def parse_correction_response(
    content: Any,
    valid_patch_ids: set,
    meeting_date=None,
    allowed_types: Optional[set] = None,
    fallback_type: str = FALLBACK_PATCH_TYPE,
) -> Optional[Tuple[Optional[str], Dict[str, Any]]]:
    """Returns (matched_patch_id_or_None, corrected_fact_value) or None
    when the response is unusable. A hallucinated patch_id (not in the
    candidate set) downgrades to unmatched rather than corrupting an
    unrelated patch."""
    obj = content
    if isinstance(obj, str):
        import json as _json
        import re as _re
        m = _re.search(r"\{.*\}", obj, _re.DOTALL)
        if not m:
            return None
        try:
            obj = _json.loads(m.group())
        except _json.JSONDecodeError:
            return None
    if not isinstance(obj, dict):
        return None

    fact = obj.get("corrected_fact")
    if not isinstance(fact, dict):
        return None
    text = fact.get("text")
    if not isinstance(text, str):
        return None
    text = " ".join(text.split())
    if not (5 <= len(text) <= 600):
        return None

    matched = obj.get("corrected_patch_id")
    if not isinstance(matched, str) or matched not in valid_patch_ids:
        matched = None

    value: Dict[str, Any] = {"text": text}
    owner = fact.get("owner")
    if isinstance(owner, str) and owner.strip():
        value["owner"] = owner.strip()
    deadline = fact.get("deadline")
    if isinstance(deadline, str) and deadline.strip():
        value["deadline"] = deadline.strip()
    dd = validate_deadline_date(fact.get("deadline_date"), meeting_date=meeting_date)
    if dd:
        value["deadline_date"] = dd

    ptype = fact.get("patch_type")
    type_set = allowed_types if allowed_types else PATCH_TYPES
    new_type = ptype if isinstance(ptype, str) and ptype in type_set else fallback_type
    value["_new_type"] = new_type  # popped by the caller; not persisted

    return matched, value


# ============================================================
# Completions from chat (contract item 10) — the sibling flow.
# A completion is NOT a correction: the fact was true and is now
# finished. Matching works identically (candidates with ids, in-block
# first) but the write path is the existing close machinery
# (completed_at + completion_source='user_chat' + evidence), so the
# patch flows the delta `completed` array and clients show "done".
# Unmatched completions are DROPPED, not stored — there is nothing to
# complete, and inventing a patch to close would manufacture memory.
# ============================================================

COMPLETION_SYSTEM = """You are the completion stage of ContextQuilt, a persistent memory system. A user has just told their assistant that something is done, closed, resolved, or finished. You are shown the user's statement and a numbered list of OPEN commitments and blockers, each with its patch_id. Your job: decide which open item (if any) the statement completes. Only pick an item the statement actually says is finished — topical similarity is not completion. If none qualifies, use null.

Respond with EXACTLY this raw JSON shape and nothing else:
{"completed_patch_id": "<patch_id copied verbatim from the list, or null>", "evidence": "<short quote or paraphrase of the user's statement showing completion, under 300 chars>", "reason": "<one short sentence>"}"""


def build_completion_content(
    statement: str,
    candidates: List[Dict[str, Any]],
    today_iso: str,
    scope_label: Optional[str] = None,
) -> str:
    """User-content block for one completion call. Candidates are dicts
    with patch_id, patch_type, text (open completables only, ordered
    in-block first)."""
    lines = [f"Current date: {today_iso}"]
    if scope_label:
        lines.append(f"Project scope: {scope_label}")
    lines.append("")
    lines.append(f"User's statement: {statement}")
    lines.append("")
    lines.append("Open items (candidates):")
    for i, c in enumerate(candidates[:MAX_CANDIDATES], 1):
        lines.append(f"{i}. [{c['patch_type']}] patch_id={c['patch_id']} :: {c['text']}")
    if not candidates:
        lines.append("(none — no open completables in this scope)")
    return "\n".join(lines)


def parse_completion_response(content: Any, valid_patch_ids: set) -> Optional[Tuple[str, str]]:
    """Returns (completed_patch_id, evidence) or None for no-match /
    hallucinated id / garbage. Unlike corrections, there is no unmatched
    fallback — None always means drop."""
    obj = content
    if isinstance(obj, str):
        import json as _json
        import re as _re
        m = _re.search(r"\{.*\}", obj, _re.DOTALL)
        if not m:
            return None
        try:
            obj = _json.loads(m.group())
        except _json.JSONDecodeError:
            return None
    if not isinstance(obj, dict):
        return None
    pid = obj.get("completed_patch_id")
    if not isinstance(pid, str) or pid not in valid_patch_ids:
        return None
    evidence = obj.get("evidence")
    evidence = " ".join(evidence.split())[:300] if isinstance(evidence, str) else ""
    return pid, evidence
