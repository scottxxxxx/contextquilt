"""Deadline micro-pass: focused second call that only resolves dates.

Why this exists (2026-07-30 coverage eval + density probe): the main
extraction call resolves weekday-relative deadlines ("by Friday",
"next Wednesday") off by one day, and adding the weekday to the
Meeting date line did not fix it — models are unreliable at
date→weekday arithmetic while juggling a 20K-token extraction task.
This pass gives a small model the one job it can do perfectly: look
dates up in a rendered calendar table.

Contract: pure functions + one orchestrator. The orchestrator never
raises into the extraction path — any failure leaves patches exactly
as the main call produced them (a possibly-imperfect date beats a
lost extraction). Every resolved date goes through the same
validate_deadline_date plausibility gate as main-call dates.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import structlog

from contextquilt.services.extraction_schema import validate_deadline_date

logger = structlog.get_logger()

# Types whose deadlines drive lifecycle (completables) or render
# staleness (goals, events). Anything else with a spoken deadline is
# still resolved — the field is universal since #180.
CALENDAR_WEEKS = 5


def build_calendar_context(meeting_date: date) -> str:
    """Render a date→weekday lookup table around the meeting.

    Starts the Monday of the meeting week and covers CALENDAR_WEEKS
    weeks — enough for "next Wednesday", "end of month", "in two
    weeks" to be lookups instead of arithmetic. One line per week
    keeps it ~10 lines / ~150 tokens.
    """
    start = meeting_date - timedelta(days=meeting_date.weekday())
    lines = [f"Meeting date: {meeting_date.isoformat()} ({meeting_date.strftime('%A')})",
             "", "Calendar (Mon..Sun per line):"]
    for w in range(CALENDAR_WEEKS):
        days = [start + timedelta(days=w * 7 + d) for d in range(7)]
        marker = "  <- meeting week" if w == 0 else ""
        lines.append(
            "  " + "  ".join(f"{d.strftime('%a')} {d.isoformat()}" for d in days) + marker
        )
    return "\n".join(lines)


def collect_deadline_items(patches: List[Dict[str, Any]]) -> List[Tuple[int, str, str]]:
    """(index, patch text, spoken deadline) for every patch that
    carries a spoken deadline. The micro-pass re-resolves ALL of them,
    not just nulls — the measured failure mode was a WRONG date, not a
    missing one, and the focused pass with a calendar is strictly more
    reliable than the inline attempt."""
    items = []
    for i, p in enumerate(patches):
        value = p.get("value")
        if not isinstance(value, dict):
            continue
        spoken = (value.get("deadline") or "").strip()
        if spoken:
            items.append((i, (value.get("text") or "")[:120], spoken))
    return items


def build_micropass_prompt(meeting_date: date, items: List[Tuple[int, str, str]]) -> Tuple[str, str]:
    """(system, user) for the resolver call. The exact output shape is
    embedded in the prompt — the Anthropic client does not enforce
    json_schema on the wire (house lesson, 2026-06)."""
    system = (
        "You resolve spoken deadlines to calendar dates. You do no other work.\n\n"
        "Rules:\n"
        "- Use ONLY the calendar table to find weekdays — never compute them.\n"
        "- 'by Friday' means the Friday of the meeting week, unless it has\n"
        "  already passed at the meeting date, then the next one.\n"
        "- 'next <weekday>' means that weekday in the week after the meeting week.\n"
        "- 'tomorrow' is the day after the meeting date; 'in two weeks' is 14\n"
        "  days after it; 'end of month' is the last day of the meeting month;\n"
        "  'end of year' is December 31 of the meeting year.\n"
        "- A date that cannot be tied to a specific day resolves to null\n"
        "  ('soon', 'after the board meeting', 'when the cert renews').\n"
        "- Absolute dates ('July 10', 'the 15th') resolve in the meeting's\n"
        "  year (or the next occurrence if that day has passed).\n\n"
        "Return ONLY a JSON object, no prose, exactly this shape:\n"
        '{"resolutions": [{"index": <int>, "deadline_date": "YYYY-MM-DD" | null}]}\n'
        "One entry per input item, same index values."
    )
    lines = [build_calendar_context(meeting_date), "", "Deadlines to resolve:"]
    for i, text, spoken in items:
        lines.append(f'- index {i}: deadline "{spoken}" (from: "{text}")')
    return system, "\n".join(lines)


def parse_micropass_response(text: str) -> Optional[List[Dict[str, Any]]]:
    """Accepts the object contract ({"resolutions": [...]}) and, for
    robustness, a bare array — models drift between the two, and the
    worker's llm.extract() parser brace-extracts OBJECTS, which is why
    the contract is object-shaped (2026-07-31 smoke: a fenced bare
    array was mangled into an unparseable brace-substring)."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        s, e = text.find(open_ch), text.rfind(close_ch)
        if s < 0 or e <= s:
            continue
        try:
            out = json.loads(text[s:e + 1])
        except Exception:
            continue
        if isinstance(out, list):
            return out
        if isinstance(out, dict):
            res = out.get("resolutions")
            if isinstance(res, list):
                return res
            for v in out.values():
                if isinstance(v, list):
                    return v
    return None


def apply_resolutions(
    patches: List[Dict[str, Any]],
    resolutions: List[Dict[str, Any]],
    meeting_date: date,
    valid_indices: set,
) -> int:
    """Write validated dates back onto value.deadline_date. Only
    indices we actually asked about are honored (a hallucinated index
    is ignored, mirroring the resolved_commitments id rule). Returns
    the count of patches updated."""
    updated = 0
    for r in resolutions:
        if not isinstance(r, dict):
            continue
        idx = r.get("index")
        if not isinstance(idx, int) or idx not in valid_indices:
            continue
        resolved = validate_deadline_date(r.get("deadline_date"), meeting_date)
        value = patches[idx].get("value")
        if not isinstance(value, dict):
            continue
        if resolved != value.get("deadline_date"):
            value["deadline_date"] = resolved
            updated += 1
    return updated


async def run_deadline_micropass(llm, patches: List[Dict[str, Any]],
                                 meeting_date: Optional[date]) -> int:
    """Orchestrate the micro-pass. Returns patches updated (0 on any
    failure or when there is nothing to resolve). Never raises."""
    if not meeting_date or not patches:
        return 0
    try:
        items = collect_deadline_items(patches)
        if not items:
            return 0
        system, user = build_micropass_prompt(meeting_date, items)
        response = await llm.extract(system_prompt=system, user_content=user)
        content = response.content
        # llm.extract parses JSON objects; the micropass returns an
        # array, which some clients surface as raw text. Handle both.
        if isinstance(content, list):
            resolutions = content
        elif isinstance(content, dict):
            res = content.get("resolutions")
            if isinstance(res, list):
                resolutions = res
            else:
                # extract() parse fallback or a wrapping model — take
                # the first list value, else give the raw dict text a
                # last chance through the tolerant parser.
                resolutions = next(
                    (v for v in content.values() if isinstance(v, list) and v), None
                ) or parse_micropass_response(json.dumps(content))
        else:
            resolutions = parse_micropass_response(str(content))
        if not resolutions:
            logger.warning("deadline_micropass_unparseable")
            return 0
        updated = apply_resolutions(
            patches, resolutions, meeting_date, {i for i, _, _ in items}
        )
        if updated:
            logger.info("deadline_micropass_applied",
                        items=len(items), updated=updated)
        return updated
    except Exception as e:
        logger.warning("deadline_micropass_failed", error=str(e)[:200])
        return 0
