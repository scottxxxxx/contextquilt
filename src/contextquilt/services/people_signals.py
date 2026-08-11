"""Per-person list signals (design 17a): the numbers behind the
situation sections, served so every client renders the same truth.

The 17a list assigns people to situations (UP NEXT, OPEN BETWEEN YOU,
DRIFTING, NEW FACES) and writes one sentence per row. CQ serves the
HONEST INPUTS, computed once here from data the list assembly already
holds; the client owns the situation assignment and the sentence
(calendar and local meeting titles are theirs). House rules
throughout: null is cannot-tell, zero is measured, counts match the
lists they open, dates bucket to days.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from statistics import median
from typing import Dict, List, Optional

# A cadence needs history: below this many distinct meeting days the
# median interval is an anecdote and serves null.
CADENCE_MIN_MEETINGS = 4


def _day(ts) -> Optional[date]:
    if isinstance(ts, datetime):
        return ts.date()
    if isinstance(ts, date):
        return ts
    return None


def compute_person_signals(
    appearances: List[dict],
    they_owe: List[dict],
    you_owe: Optional[List[dict]],
    today: Optional[date] = None,
) -> Dict:
    """The `signals` block for one person row.

    appearances: [{last_seen_at, turn_count}] (one per meeting, as the
    list assembly fetches them). they_owe / you_owe: the SAME item rows
    the ledger serves (shelved already excluded), so every count here
    agrees with the list it opens; you_owe None passes through as
    cannot-tell per the owed_to capability.
    """
    today = today or datetime.now(timezone.utc).date()

    # A mention is not a meeting (the RV/Raj field report, 2026-08-11):
    # someone NAMED in a room did not attend it, and "met 2 hours ago"
    # is a presence claim. Presence-grade = speaker or ownership
    # capacity; EMPTY capacities are pre-migration-31 rows and count as
    # presence per that migration's own rule (unknown must not become
    # "did not attend"). Mention-only rows still exist on the person
    # (the directory knows the name) but never feed a presence number.
    def _present(a) -> bool:
        caps = set(a.get("capacities") or [])
        return not caps or bool(caps & {"speaker", "ownership"})

    present = [a for a in appearances if _present(a)]
    present_days = sorted(
        {d for a in present if (d := _day(a.get("last_seen_at")))}
    )
    appearances = present
    days = present_days
    meetings_7d = sum(1 for d in days if (today - d).days < 7)
    meetings_30d = sum(1 for d in days if (today - d).days < 30)
    turns = [
        a.get("turn_count") for a in appearances
        if a.get("turn_count") is not None
        and (d := _day(a.get("last_seen_at"))) and (today - d).days < 30
    ]
    turns_30d = sum(turns) if turns else None

    cadence = None
    if len(days) >= CADENCE_MIN_MEETINGS:
        intervals = [(b - a).days for a, b in zip(days, days[1:]) if (b - a).days > 0]
        if intervals:
            cadence = {
                "median_interval_days": round(median(intervals)),
                "meetings_observed": len(days),
            }

    def _overdue(item) -> bool:
        dd = item.get("deadline_date")
        return bool(dd) and str(dd) < today.isoformat()

    def _summ(item, direction):
        v = item.get("value") or {}
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except (ValueError, TypeError):
                v = {}
        text = item.get("text") or v.get("text") or ""
        return {
            "direction": direction,
            "patch_id": str(item["patch_id"]) if item.get("patch_id") else None,
            "text": text,
            "deadline_date": item.get("deadline_date"),
            "overdue": _overdue(item),
        }

    candidates = [(i, "they_owe") for i in they_owe]
    if you_owe:
        candidates += [(i, "you_owe") for i in you_owe]
    # Most urgent first, deterministically: overdue beats dated beats
    # undated; earlier deadline beats later; patch id breaks ties.
    candidates.sort(key=lambda c: (
        not _overdue(c[0]),
        c[0].get("deadline_date") is None,
        str(c[0].get("deadline_date") or "9999"),
        str(c[0].get("patch_id") or ""),
    ))
    next_item = _summ(*candidates[0]) if candidates else None

    return {
        "meetings_7d": meetings_7d,
        "meetings_30d": meetings_30d,
        "turns_30d": turns_30d,
        "cadence": cadence,
        # Presence anchors for "met X ago" copy and NEW FACES gating:
        # null = never actually present (mention-only person), which is
        # a different claim from "met long ago" and must render as one.
        "first_present_at": days[0].isoformat() if days else None,
        "last_present_at": days[-1].isoformat() if days else None,
        "open_between": {
            "they_owe_open": len(they_owe),
            "they_owe_overdue": sum(1 for i in they_owe if _overdue(i)),
            # Null when the caller's manifest lacks owed_to: cannot-tell,
            # never rendered as "you owe nothing".
            "you_owe_open": None if you_owe is None else len(you_owe),
            "next_open_item": next_item,
        },
    }
