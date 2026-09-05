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


# Token budget for the rendered context block (GP contract, 2026-06-11):
# GP's system prompt scaffold reserves 8000 tokens total across
# instructions, summary, project context, and our block. Default sits in
# their requested 600-800 band; callers tune per request via
# metadata.token_budget on /v1/recall (Project Chat on big models wants a
# richer block than a quick mid-meeting prompt). The formatter works in
# characters; ~4 chars/token is the repo's standing heuristic (same one
# the worker's queue budgeting uses).
# Section labels for grouped mode. The endpoint overrides these per
# locale; anything a locale omits falls back to the English here.
_DEFAULT_LABELS = {
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

DEFAULT_RECALL_TOKEN_BUDGET = 700
MIN_RECALL_TOKEN_BUDGET = 100
MAX_RECALL_TOKEN_BUDGET = 2000
CHARS_PER_TOKEN = 4


def resolve_token_budget(metadata: "Optional[Dict[str, Any]]") -> int:
    """Resolve metadata.token_budget to a clamped int, defaulting on
    anything missing or malformed. Never raises — recall must not 4xx
    over a bad tuning knob."""
    raw = (metadata or {}).get("token_budget")
    try:
        budget = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_RECALL_TOKEN_BUDGET
    return max(MIN_RECALL_TOKEN_BUDGET, min(MAX_RECALL_TOKEN_BUDGET, budget))


# Recall age window (tier contract, 2026-08-21): metadata.max_age_days
# bounds MEETING-BOUND memory to the last N days; universal self-
# disclosure types (the manifest's universal_recall_types) are exempt
# because a preference does not expire on day 31. The number is owned by
# the gateway's tier config and never defaulted here: absent means no
# window, which is the pre-window behavior byte for byte. Clamped above
# so a typo cannot request a multi-century window that forces a full
# scan for no reason.
MAX_RECALL_AGE_DAYS = 3650


def resolve_max_age_days(metadata: "Optional[Dict[str, Any]]") -> Optional[int]:
    """Resolve metadata.max_age_days to a positive int, or None for no
    window. Never raises and never defaults to a number: a malformed or
    non-positive value means "no window", never a 4xx, same posture as
    resolve_token_budget. Booleans are rejected explicitly because
    int(True) == 1 would turn a stray `true` into a one-day window."""
    raw = (metadata or {}).get("max_age_days")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return None
    if days < 1:
        return None
    return min(MAX_RECALL_AGE_DAYS, days)


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
        # "not confirmed done" is what recall actually knows: nothing
        # auto-closes (2026-08-18 ruling), so a past due row may have been
        # delivered and never marked. Persona test 2026-09-05: four rows
        # rendered OVERDUE at the top of the block, three of them finished
        # in the very transcript under discussion, and the assistant
        # chased them.
        return "OVERDUE, not confirmed done"
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
    """Back-compat wrapper — see format_flat_ranked_with_stats."""
    context, _ = format_flat_ranked_with_stats(
        scored_patches, entity_rows, relationship_rows, max_chars=max_chars, today=today
    )
    return context


def format_flat_ranked_with_stats(
    scored_patches: Sequence[Tuple[float, Any]],
    entity_rows: Sequence[Any],
    relationship_rows: Sequence[Any],
    max_chars: int = 1600,
    today: Optional[date] = None,
    person_entity_type: str = "person",
    conduct_types: "frozenset" = frozenset(),
    capsule_limit: int = 3,
) -> Tuple[str, int]:
    """Format patches as a flat relevance-ranked list.

    Conduct rows (origin scoped, not project scoped: SS's moment) whose
    owner is a person in the header are folded into that person's line
    as a capsule, "how they operate: a; b; c", newest first, up to
    `capsule_limit`, and leave the list. A conduct row about somebody
    the query did not name stays in the list at its own rank. The
    prompt wants one line per named person, not three rows of conduct
    competing with commitments for slots (persona test 2026-09-05).

    Targets roughly 150-300 tokens in total. Includes a compact
    people/projects header (from the matched entity rows), then a
    single flat list of patches ordered by score.

    max_chars is a soft cap — output is truncated at the next
    patch boundary once the budget is reached. Default 1600 chars
    ≈ 400 tokens, enough for ~10-15 patches.

    Returns (context, rendered_patch_lines) — the count feeds the
    coverage line (contract commitment E): truncation must be visible,
    a window must never read as the whole memory.
    """
    sections: List[str] = []

    # Compact header: people and projects matched in the query. Which
    # entity type IS "a person" comes from the caller's vocabulary (SS
    # default "person"); a vocab app's person entities would otherwise
    # vanish from the header while their relationships still rendered.
    people = [r for r in entity_rows if (r.get("entity_type") if isinstance(r, dict) else r["entity_type"]) == person_entity_type]
    projects = [r for r in entity_rows if (r.get("entity_type") if isinstance(r, dict) else r["entity_type"]) == "project"]

    # Conduct rows fold into the person they are about.
    capsules: Dict[str, List[str]] = {}
    folded: set = set()
    if conduct_types and people:
        person_names = [(p["name"] if isinstance(p, dict) else p.get("name", "")) or "" for p in people]
        for score, row in scored_patches:
            ptype = row["patch_type"] if isinstance(row, dict) else row.get("patch_type")
            if ptype not in conduct_types:
                continue
            v = _parse_value(row)
            owner = (v.get("owner") or "").strip()
            text = (v.get("text") or "").strip()
            if not owner or not text:
                continue
            for name in person_names:
                if _same_person(owner, name):
                    lines = capsules.setdefault(name, [])
                    # Only a row that made the capsule leaves the list. The
                    # first deploy folded every matched row and the fourth
                    # of Raj's interview questions vanished from the block
                    # entirely (prod smoke, 2026-09-05); overflow keeps its
                    # own low rank in the list instead.
                    if len(lines) < capsule_limit:
                        lines.append(text)
                        folded.add(id(row))
                    break

    if projects or people:
        header_parts: List[str] = []
        if projects:
            names = [_entity_name_with_desc(p) for p in projects]
            header_parts.append("Projects: " + "; ".join(names))
        if people:
            names = []
            for p in people:
                name = (p["name"] if isinstance(p, dict) else p.get("name", "")) or ""
                line = _entity_name_with_desc(p)
                if capsules.get(name):
                    # " / " inside a capsule, because "; " already separates
                    # people on this line and the two read as one list.
                    line += ". How they operate: " + " / ".join(capsules[name])
                names.append(line)
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
        if id(row) in folded:
            continue
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

    return "\n\n".join(sections), len(patch_lines)


def _same_person(owner: str, name: str) -> bool:
    """The row's owner is this header person: same name, or the same
    first token when one side is a bare first name. Never a substring."""
    o, n = owner.strip().lower(), name.strip().lower()
    if not o or not n:
        return False
    if o == n:
        return True
    return o.split(" ")[0] == n.split(" ")[0]


def _entity_name_with_desc(row: Any) -> str:
    name = row["name"] if isinstance(row, dict) else row.get("name", "")
    desc = row["description"] if isinstance(row, dict) else row.get("description")
    if desc:
        return f"{name} ({desc})"
    return name


# The age marker (the Vijay lesson, 2026-08-11): recall serves durable
# memory with no time scope, and an episodic claim five weeks old reads
# exactly like yesterday's unless the line SAYS when it was observed. A
# report asked for "the last two weeks" prominently repeated a July
# "unreachable since" blocker about a person who had been in six
# meetings that fortnight; the model was faithful to an undated line.
#
# Presentation only, deliberately simpler than the decay model: the
# bands need registry TTLs and update anchors the hot path neither
# fetches nor may query, while the observation date is already on every
# recall row. Self-typed types are exempt (their freshness model
# already penalizes rank, and a durable trait wearing an old date would
# invite doubt the type does not deserve). Dates only, so output stays
# byte-stable within a UTC day; a marker appears once, on the day the
# threshold crosses, the same rhythm as the deadline markers.
AGE_MARKER_DAYS = 28
AGE_MARKER_EXEMPT_TYPES = frozenset(
    {"trait", "preference", "goal", "constraint", "identity"}
)


def _render_observed_age(
    row: Any, ptype: str, today: date
) -> str:
    if ptype in AGE_MARKER_EXEMPT_TYPES:
        return ""
    ts = None
    for key in ("last_observed_at", "created_at"):
        try:
            ts = row[key] if isinstance(row, dict) else row.get(key)
        except (KeyError, TypeError):
            ts = None
        if ts is not None:
            break
    if ts is None:
        return ""
    observed = ts.date() if isinstance(ts, datetime) else ts
    if not isinstance(observed, date):
        return ""
    if (today - observed).days < AGE_MARKER_DAYS:
        return ""
    return f"last observed {observed.isoformat()}"


def _format_patch_line(row: Any, today: Optional[date] = None) -> str:
    """One-line representation of a patch for flat output."""
    ptype = row["patch_type"] if isinstance(row, dict) else row.get("patch_type")
    v = _parse_value(row)
    text = v.get("text", "").strip()
    if not text:
        return ""

    owner = (v.get("owner") or "").strip()
    deadline_fragment = _render_deadline(v, today or _today_utc())
    age_fragment = _render_observed_age(row, ptype, today or _today_utc())

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
    if age_fragment:
        detail_parts.append(age_fragment)
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
    person_entity_type: str = "person",
) -> str:
    """Format patches in the pre-PR-4 category-grouped structure.

    `labels` is the i18n label dict from the recall endpoint. When
    omitted, sensible English defaults are used.
    """
    # Merged over the defaults rather than replacing them. The endpoint
    # passes a LOCALE table, and a locale table that is missing a key the
    # formatter indexes directly used to raise KeyError, which main.py
    # then swallowed into an empty context block. Merging means a missing
    # key degrades to its English label instead of erasing the whole
    # block, and adding a key to one locale can never break another.
    labels = {**_DEFAULT_LABELS, **(labels or {})}
    sections: List[str] = []

    people = [r for r in entity_rows if (r.get("entity_type") if isinstance(r, dict) else r["entity_type"]) == person_entity_type]
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
    # Goals + constraints (new facets). Goals get deadline decoration —
    # a dated goal past its date must read as stale, matching the flat
    # formatter, which renders the fragment for any type carrying one.
    goals = buckets.get("goal", [])
    if goals:
        today = today or _today_utc()
        goal_lines: List[str] = []
        for v in goals:
            fragment = _render_deadline(v, today)
            dl = f" ({fragment})" if fragment else ""
            goal_lines.append(f"- {v.get('text', '')}{dl}")
        sections.append(f"{labels['goals']}:\n" + "\n".join(goal_lines))
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


# ============================================================
# People-scoped recall (boundary piece 4, decision 2026-08-11)
# ============================================================

def format_people_scope(
    people_rows: Sequence[Dict[str, Any]],
    entity_rows: Sequence[Any],
    relationship_rows: Sequence[Any],
    today: Optional[date] = None,
    person_entity_type: str = "person",
    max_open_per_person: int = 10,
    max_completed_per_person: int = 5,
) -> Tuple[str, List[str], int]:
    """The free-tier recall render: exactly what the People tab shows.

    The screens rule made concrete: identity and relations (the header
    the flat mode already renders), then each matched person's ledger,
    open items with the same [todo]/[blocker] lines and deadline markers
    the paid render uses, the you_owe side when the app's manifest
    tracks it, and recently completed items. NOTHING memory-side: no
    universal facts, no cues, no metamemory, no synthesis.

    `people_rows` are _people_core rows (the same assembly the People
    tab is served from, which is what makes the equivalence true rather
    than aspirational). Deterministic: people sorted by name, items in
    the core's stable order, caps self-describing so a truncated ledger
    never reads as the whole ledger.

    Returns (context, rendered_patch_ids, total_ledger_items).
    """
    today = today or _today_utc()

    header, _ = format_flat_ranked_with_stats(
        [], entity_rows, relationship_rows,
        today=today, person_entity_type=person_entity_type,
    )
    sections: List[str] = [header] if header else []
    rendered_ids: List[str] = []
    total_items = 0

    def _line(r: Dict[str, Any]) -> str:
        return _format_patch_line(
            {"patch_type": r["patch_type"], "value": {
                "text": r["text"], "owner": r["owner"],
                "deadline": r["deadline"], "deadline_date": r["deadline_date"],
                "overdue_since": r["overdue_since"],
            }},
            today,
        )

    for person in sorted(people_rows, key=lambda p: (p["name"] or "", p["entity_id"])):
        name = person["name"]
        they = person.get("_they_owe") or []
        total_items += len(they)
        if they:
            shown = they[:max_open_per_person]
            lines = [f"{name} owes you ({len(they)} open):"]
            lines += [_line(r) for r in shown]
            if len(they) > len(shown):
                lines.append(f"(showing {len(shown)} of {len(they)} open)")
            sections.append("\n".join(lines))
            rendered_ids += [str(r["patch_id"]) for r in shown]

        you = person.get("_you_owe")
        if you:
            total_items += len(you)
            lines = [f"You owe {name} ({len(you)} open):"]
            lines += [_line(r) for r in you[:max_open_per_person]]
            sections.append("\n".join(lines))
            rendered_ids += [str(r["patch_id"]) for r in you[:max_open_per_person]]

        done = person.get("_completed_they_owe") or []
        total_items += len(done)
        if done:
            shown = done[:max_completed_per_person]
            lines = [f"Recently completed by {name} ({len(done)} total):"]
            for r in shown:
                stamp = (r.get("completed_at").date().isoformat()
                         if r.get("completed_at") else "")
                suffix = f" [done {stamp}]" if stamp else " [done]"
                lines.append(_line(r) + suffix)
            sections.append("\n".join(lines))
            rendered_ids += [str(r["patch_id"]) for r in shown]

    return "\n\n".join(s for s in sections if s), rendered_ids, total_items
