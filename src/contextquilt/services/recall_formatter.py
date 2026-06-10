"""
Recall output formatters.

Two modes:
  - flat (default): relevance-ranked list, minimal adornment, compact.
    Each patch rendered on one line with just enough context for the
    LLM (type, text, owner/deadline if relevant). Query-scoped.

  - grouped: category-grouped block with section headers ("About you",
    "Open commitments", etc.). Backward-compatible with the pre-PR-4
    recall output.

Both take the output of recall_scorer.score_patches plus the entity
match + graph traversal rows from the recall endpoint.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


def _today_utc() -> date:
    """Day-grain 'now' for deadline status. Day granularity keeps the
    rendered context byte-stable across a UTC day, matching the scorer's
    day-bucketed freshness clock (required for upstream prompt caching)."""
    return datetime.now(timezone.utc).date()


def _deadline_status(deadline_date: str, today: date) -> Optional[str]:
    """Urgency marker for a structured deadline. None means no marker
    (far-future deadline, or unparseable string)."""
    try:
        parsed = date.fromisoformat(deadline_date)
    except (TypeError, ValueError):
        return None
    delta_days = (parsed - today).days
    if delta_days < 0:
        return "OVERDUE"
    if delta_days == 0:
        return "due today"
    if delta_days <= 7:
        return "due soon"
    return None


def _render_deadline(v: Dict[str, Any], today: date) -> str:
    """Deadline fragment for a patch value, preferring the structured
    `deadline_date` (with urgency marker) over the spoken free text."""
    deadline_date = (v.get("deadline_date") or "").strip()
    if deadline_date:
        status = _deadline_status(deadline_date, today)
        if status:
            return f"by {deadline_date} ({status})"
        return f"by {deadline_date}"
    deadline = (v.get("deadline") or "").strip()
    if deadline:
        return f"by {deadline}"
    return ""


def _parse_value(row: Any) -> Dict[str, Any]:
    v = row["value"] if isinstance(row, dict) else row["value"]
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except Exception:
            return {}
    return v if isinstance(v, dict) else {}


# ============================================================
# Flat / query-scoped formatter (PR 4 default)
# ============================================================


def format_flat_ranked(
    scored_patches: Sequence[Tuple[float, Any]],
    entity_rows: Sequence[Any],
    relationship_rows: Sequence[Any],
    max_chars: int = 1600,
    today: Optional[date] = None,
) -> str:
    """Format patches as a flat relevance-ranked list.

    Targets roughly 150-300 tokens in total. Includes a compact
    people/projects header (from the matched entity rows), then a
    single flat list of patches ordered by score.

    max_chars is a soft cap — output is truncated at the next
    patch boundary once the budget is reached. Default 1600 chars
    ≈ 400 tokens, enough for ~10-15 patches.
    """
    sections: List[str] = []

    # Compact header: people and projects matched in the query
    people = [r for r in entity_rows if (r.get("entity_type") if isinstance(r, dict) else r["entity_type"]) == "person"]
    projects = [r for r in entity_rows if (r.get("entity_type") if isinstance(r, dict) else r["entity_type"]) == "project"]

    if projects or people:
        header_parts: List[str] = []
        if projects:
            names = [_entity_name_with_desc(p) for p in projects]
            header_parts.append("Projects: " + "; ".join(names))
        if people:
            names = [_entity_name_with_desc(p) for p in people]
            header_parts.append("People: " + "; ".join(names))
        sections.append("\n".join(header_parts))

    # Relationships — only surface if we have any and they're short
    if relationship_rows:
        rel_lines: List[str] = []
        for r in relationship_rows[:5]:
            from_name = r["from_name"] if isinstance(r, dict) else r.get("from_name")
            to_name = r["to_name"] if isinstance(r, dict) else r.get("to_name")
            rel_type = r["relationship_type"] if isinstance(r, dict) else r.get("relationship_type")
            if from_name and to_name and rel_type:
                rel_lines.append(f"{from_name} {rel_type} {to_name}")
        if rel_lines:
            sections.append("Relations: " + "; ".join(rel_lines))

    # Flat list of patches — one per line, ranked
    today = today or _today_utc()
    patch_lines: List[str] = []
    remaining = max_chars - sum(len(s) for s in sections) - 20  # small buffer
    for score, row in scored_patches:
        line = _format_patch_line(row, today)
        if not line:
            continue
        if remaining <= 0:
            break
        if len(line) + 2 > remaining:
            break
        patch_lines.append(line)
        remaining -= (len(line) + 2)  # +2 for joining newlines

    if patch_lines:
        sections.append("\n".join(patch_lines))

    return "\n\n".join(sections)


def _entity_name_with_desc(row: Any) -> str:
    name = row["name"] if isinstance(row, dict) else row.get("name", "")
    desc = row["description"] if isinstance(row, dict) else row.get("description")
    if desc:
        return f"{name} ({desc})"
    return name


def _format_patch_line(row: Any, today: Optional[date] = None) -> str:
    """One-line representation of a patch for flat output."""
    ptype = row["patch_type"] if isinstance(row, dict) else row.get("patch_type")
    v = _parse_value(row)
    text = v.get("text", "").strip()
    if not text:
        return ""

    owner = (v.get("owner") or "").strip()
    deadline_fragment = _render_deadline(v, today or _today_utc())

    prefix_map = {
        "trait": "about you",
        "preference": "pref",
        "goal": "goal",
        "constraint": "rule",
        "decision": "decided",
        "commitment": "todo",
        "blocker": "blocker",
        "takeaway": "note",
        "event": "event",
        "role": "role",
        "person": "person",
        "org": "org",
        "project": "project",
    }
    prefix = prefix_map.get(ptype, ptype or "fact")

    detail_parts: List[str] = []
    if owner:
        detail_parts.append(f"owner: {owner}")
    if deadline_fragment:
        detail_parts.append(deadline_fragment)
    suffix = f" [{', '.join(detail_parts)}]" if detail_parts else ""

    return f"[{prefix}] {text}{suffix}"


# ============================================================
# Grouped / category formatter (retained for backward compat)
# ============================================================


def format_category_grouped(
    scored_patches: Sequence[Tuple[float, Any]],
    entity_rows: Sequence[Any],
    relationship_rows: Sequence[Any],
    labels: Optional[Dict[str, str]] = None,
    today: Optional[date] = None,
) -> str:
    """Format patches in the pre-PR-4 category-grouped structure.

    `labels` is the i18n label dict from the recall endpoint. When
    omitted, sensible English defaults are used.
    """
    labels = labels or {
        "project": "Project",
        "people": "People",
        "connections": "Connections",
        "about_you": "About you",
        "decisions": "Decisions",
        "commitments": "Open commitments",
        "blockers": "Blockers",
        "roles": "Roles",
        "key_facts": "Key facts",
        "goals": "Goals",
        "constraints": "Constraints",
        "events": "Recent events",
    }
    sections: List[str] = []

    people = [r for r in entity_rows if (r.get("entity_type") if isinstance(r, dict) else r["entity_type"]) == "person"]
    projects = [r for r in entity_rows if (r.get("entity_type") if isinstance(r, dict) else r["entity_type"]) == "project"]
    if projects:
        for p in projects:
            name = p["name"] if isinstance(p, dict) else p.get("name", "")
            desc = p["description"] if isinstance(p, dict) else p.get("description")
            sections.append(f"{labels['project']}: {name} — {desc or ''}")
    if people:
        people_str = ", ".join(_entity_name_with_desc(p) for p in people)
        sections.append(f"{labels['people']}: {people_str}")

    if relationship_rows:
        rel_lines: List[str] = []
        for r in relationship_rows:
            from_name = r["from_name"] if isinstance(r, dict) else r.get("from_name")
            to_name = r["to_name"] if isinstance(r, dict) else r.get("to_name")
            rel_type = r["relationship_type"] if isinstance(r, dict) else r.get("relationship_type")
            ctx = r["context"] if isinstance(r, dict) else r.get("context")
            if from_name and to_name and rel_type:
                line = f"{from_name} {rel_type} {to_name}"
                if ctx:
                    line += f" ({ctx})"
                rel_lines.append(line)
        if rel_lines:
            sections.append(f"{labels['connections']}:\n" + "\n".join(f"- {l}" for l in rel_lines))

    # Bucket patches by type
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for _, row in scored_patches:
        ptype = row["patch_type"] if isinstance(row, dict) else row.get("patch_type") or ""
        buckets.setdefault(ptype, []).append(_parse_value(row))

    def render_bucket(section_key: str, types: Iterable[str]) -> None:
        items: List[Dict[str, Any]] = []
        for t in types:
            items.extend(buckets.get(t, []))
        if not items:
            return
        sections.append(
            f"{labels.get(section_key, section_key.title())}:\n"
            + "\n".join(f"- {v.get('text', '')}" for v in items)
        )

    # About you
    render_bucket("about_you", ("trait", "preference"))
    # Goals + constraints (new facets)
    render_bucket("goals", ("goal",))
    render_bucket("constraints", ("constraint",))
    # Decisions
    render_bucket("decisions", ("decision",))
    # Open commitments — owner/deadline decoration
    commitments = buckets.get("commitment", [])
    if commitments:
        today = today or _today_utc()
        lines: List[str] = []
        for v in commitments:
            owner = v.get("owner", "")
            deadline_fragment = _render_deadline(v, today)
            dl = f" ({deadline_fragment})" if deadline_fragment else ""
            prefix = f"{owner}: " if owner else ""
            lines.append(f"- {prefix}{v.get('text', '')}{dl}")
        sections.append(f"{labels['commitments']}:\n" + "\n".join(lines))
    # Blockers
    render_bucket("blockers", ("blocker",))
    # Roles
    render_bucket("roles", ("role",))
    # Events
    render_bucket("events", ("event",))
    # Key facts (takeaways, people, orgs, misc)
    render_bucket("key_facts", ("takeaway", "person", "org"))

    return "\n\n".join(sections)
