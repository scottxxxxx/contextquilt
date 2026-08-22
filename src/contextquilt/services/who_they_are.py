"""Who they are: one synthesis of a person across everything they have
stated and everything meetings have shown, written over time.

Scott, 2026-08-21: "I want to see the history of what we think their
role is and how it has changed over time, and how we can average it out
to get the best comprehensive summary that goes beyond a simple title."

The title (#301) answers the first half with a rule: a role the person
STATED beats a description a meeting INFERRED. This module answers the
second half, and it is deliberately the only model-bearing step in the
description story, because the 2026-08-13 person-analyzer experiment
showed that cross-meeting synthesis is the one thing a model adds that
code cannot, and also that Haiku fails it invisibly (missed the
cross-meeting pattern and fabricated a receipt) while Sonnet does not.
So: the arithmetic is here, in code; the model writes prose from inputs
it was handed and nothing else; and the lens runs on its own model.

What the model is given, and all it is given:
  R1..Rn  stated roles, newest first: text, project, date
  P1..Pn  perceptions from entity_descriptions, newest first: text,
          first seen, last seen, times confirmed
  the person's meeting count, first and last seen, and projects

What comes back is checked, not trusted:
  - every number in the prose must be one it was handed
  - if a stated role exists, the newest one's title phrase must appear
    in the summary, because a synthesis that drops what the person said
    about themselves in favour of what a meeting inferred has reversed
    the precedence rule this whole feature exists to enforce
  - the cited sources must be ids it was given
  - no dash-as-punctuation, no opening with the person's name
  - length caps, enforced in the parse

A rejected answer costs one call and no write. The fingerprint of the
inputs is stored with the card, so the pass regenerates only when the
inputs change, and a person described the same way forty times costs
one call, not forty.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

from contextquilt.services.insight_cards import dash_as_punctuation, opens_with_name

LENS = "who_they_are"

# Served as its own field on person detail, NOT in the insights card
# stack: the stack's capsule is a one-line teaser with a 180-char claim
# and this is a short paragraph. The read filters this lens out of
# `insights` and serves it as `who_they_are`.
MAX_SUMMARY_CHARS = 600
MIN_SUMMARY_CHARS = 40
MAX_TRAJECTORY_CHARS = 300

# Eligibility: something stated, or enough observed to have a shape.
MIN_PERCEPTIONS_WITHOUT_ROLE = 2

# The model for this lens. Sonnet by default; the 08-13 experiment is
# the receipt. Operator override for cost experiments, kill switch
# separate.
DEFAULT_MODEL = "claude-sonnet-4-6"

_INTEGER = re.compile(r"\d+")
_PUNCT = re.compile(r"[^\w\s]")


def _norm(text: str) -> str:
    """Lowercase, punctuation stripped, whitespace collapsed. The stated
    role check compares on this, because the first prod cycle rejected
    "Described as the new HR manager for the West Coast region, a role
    stated..." against a role text that ended in a period."""
    return " ".join(_PUNCT.sub(" ", (text or "").lower()).split())


_ROLE_LEADS = (" is ", " was ", " serves as ", " works as ", " acts as ", ": ")


def eligible(stated_roles: Sequence[Mapping[str, Any]],
             perceptions: Sequence[Mapping[str, Any]]) -> bool:
    if stated_roles:
        return True
    return len(perceptions) >= MIN_PERCEPTIONS_WITHOUT_ROLE


def _date(v: Any) -> Optional[str]:
    if v is None:
        return None
    if hasattr(v, "date"):
        try:
            return v.date().isoformat()
        except Exception:  # pragma: no cover
            pass
    s = str(v)
    return s[:10] if len(s) >= 10 else s


def build_facts(
    person_name: str,
    stated_roles: Sequence[Mapping[str, Any]],
    perceptions: Sequence[Mapping[str, Any]],
    meeting_count: int,
    first_seen: Any,
    last_seen: Any,
    projects: Sequence[str],
) -> Dict[str, Any]:
    """The arithmetic, in code. Ids are positional and stable for one
    set of inputs; the fingerprint covers everything that can change
    the prose."""
    roles = [
        {
            "id": f"R{i + 1}",
            "patch_id": str(r.get("patch_id")) if r.get("patch_id") else None,
            "text": (r.get("text") or "").strip(),
            "project": r.get("project"),
            "date": _date(r.get("stated_at")),
            "origin_id": r.get("origin_id"),
        }
        for i, r in enumerate(stated_roles)
        if (r.get("text") or "").strip()
    ]
    percs = [
        {
            "id": f"P{i + 1}",
            "text": (p.get("description") or "").strip(),
            "first_seen": _date(p.get("first_observed_at")),
            "last_seen": _date(p.get("last_observed_at")),
            "times": int(p.get("observation_count") or 1),
            "origin_id": p.get("first_origin_id"),
        }
        for i, p in enumerate(perceptions)
        if (p.get("description") or "").strip()
    ]
    facts = {
        "person": person_name,
        "roles": roles,
        "perceptions": percs,
        "meeting_count": int(meeting_count or 0),
        "first_seen": _date(first_seen),
        "last_seen": _date(last_seen),
        "projects": [p for p in projects if p][:8],
        "distinct_perceptions": len(percs),
        "total_confirmations": sum(p["times"] for p in percs),
    }
    facts["fingerprint"] = fingerprint(facts)
    return facts


def fingerprint(facts: Mapping[str, Any]) -> str:
    """What the prose depends on. Dates of perceptions are included
    because "how it changed over time" is part of the answer; the
    meeting count is NOT, so a card does not churn on every meeting in
    which nothing about the person's role moved."""
    basis = {
        "roles": [(r["text"], r["project"], r["date"]) for r in facts.get("roles", [])],
        "perceptions": [(p["text"], p["first_seen"], p["last_seen"], p["times"])
                        for p in facts.get("perceptions", [])],
        "projects": list(facts.get("projects", [])),
    }
    return hashlib.sha256(json.dumps(basis, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def title_phrase(role_text: str, person_name: str) -> str:
    """The same strip the served `title` uses, so the check below asks
    for exactly the phrase a client shows under the name."""
    raw = (role_text or "").strip()
    low = raw.lower()
    for n in sorted({person_name.lower(), person_name.split(" ")[0].lower()}, key=len, reverse=True):
        if n and low.startswith(n):
            rest = raw[len(n):]
            rl = rest.lower()
            for lead in _ROLE_LEADS:
                if (rl + " ").startswith(lead):
                    return rest[len(lead):].strip()
            return rest.lstrip(", (").rstrip(")").strip()
    return raw


def allowed_numbers(facts: Mapping[str, Any]) -> set:
    nums = {str(facts.get("meeting_count", 0)), str(facts.get("distinct_perceptions", 0)),
            str(facts.get("total_confirmations", 0))}
    for p in facts.get("perceptions", []):
        nums.add(str(p["times"]))
    for r in facts.get("roles", []):
        for d in (r.get("date"),):
            if d:
                nums.update(_INTEGER.findall(d))
    for p in facts.get("perceptions", []):
        for d in (p.get("first_seen"), p.get("last_seen")):
            if d:
                nums.update(_INTEGER.findall(d))
    for d in (facts.get("first_seen"), facts.get("last_seen")):
        if d:
            nums.update(_INTEGER.findall(d))
    return nums


SYSTEM = """You are the memory-consolidation stage of ContextQuilt, a persistent memory system. You are given everything the system knows about how ONE person has been described over time: the roles they STATED about themselves, and the PERCEPTIONS each meeting produced, each with dates and how many times it was confirmed. ContextQuilt computed all of it. Your job is to write who this person is to the user, as a short synthesis that goes beyond a title and says how the picture has moved.

Rules, every one enforced after you answer:
1. A role the person STATED beats a perception a meeting inferred. If any stated role exists, the newest one's title phrase MUST appear in your summary, word for word.
2. Use only the facts given. No number that was not handed to you. No title, employer, seniority or affiliation that no input states. If the inputs are thin, say less.
3. Say how it changed over time when it did: what they were first seen as, what they are now, and whether the perceptions agree with what they stated. Use the dates given, by month and year is fine.
4. Two or three sentences for the summary. One sentence for the trajectory, or null if nothing moved.
5. Do not open with the person's name. Do not use a dash as punctuation anywhere; use a comma, a colon, or two sentences.
6. Cite every input you drew on by its id.

Answer with raw JSON only, exactly this shape and nothing else:
{"summary": "<2 to 3 sentences>", "trajectory": "<1 sentence or null>", "sources": ["R1", "P2"], "output_language": "<BCP-47 of the inputs>"}"""


def build_content(facts: Mapping[str, Any], used_openings: Sequence[str] = ()) -> str:
    lines = [f"PERSON: {facts['person']}",
             f"MEETINGS WITH THEM: {facts['meeting_count']} (first {facts.get('first_seen')}, last {facts.get('last_seen')})"]
    if facts.get("projects"):
        lines.append("PROJECTS: " + ", ".join(facts["projects"]))
    lines.append("")
    lines.append("STATED ROLES, newest first (what they said about themselves):")
    if facts["roles"]:
        for r in facts["roles"]:
            proj = f" [project: {r['project']}]" if r.get("project") else ""
            lines.append(f"  {r['id']}: \"{r['text']}\"{proj} (stated {r['date']})")
    else:
        lines.append("  none")
    lines.append("")
    lines.append("PERCEPTIONS, newest first (what a meeting showed them doing):")
    for p in facts["perceptions"]:
        span = p["first_seen"] if p["first_seen"] == p["last_seen"] else f"{p['first_seen']} to {p['last_seen']}"
        lines.append(f"  {p['id']}: \"{p['text']}\" (seen {span}, confirmed {p['times']}x)")
    if not facts["perceptions"]:
        lines.append("  none")
    if used_openings:
        lines.append("")
        lines.append("Other people's summaries already open with these words; do not open with any of them: "
                     + "; ".join(f"\"{u}\"" for u in used_openings))
    return "\n".join(lines)


def parse_response(
    raw: str, facts: Mapping[str, Any], defects: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Validate or decline. Returns {summary, trajectory, sources,
    output_language} or None, appending the first defect found."""
    defects = defects if defects is not None else []
    # The LLM clients parse JSON before handing it over: `content` is a
    # dict when the model answered cleanly and a string only in the
    # fixture stub. Receipt 2026-08-21 22:52Z: the first prod cycle
    # failed on every person with "'dict' object has no attribute
    # 'strip'" because this parse assumed text. Accept both.
    if isinstance(raw, dict):
        if raw.get("_parse_error"):
            defects.append("not_json")
            return None
        obj = raw
        text = ""
    else:
        text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    if text:
        try:
            obj = json.loads(text)
        except Exception:
            m = re.search(r"\{.*\}", text, re.S)
            if not m:
                defects.append("not_json")
                return None
            try:
                obj = json.loads(m.group(0))
            except Exception:
                defects.append("not_json")
                return None
    summary = (obj.get("summary") or "").strip() if isinstance(obj, dict) else ""
    trajectory = obj.get("trajectory") if isinstance(obj, dict) else None
    trajectory = trajectory.strip() if isinstance(trajectory, str) and trajectory.strip() else None
    sources = obj.get("sources") if isinstance(obj, dict) else None
    if not summary:
        defects.append("no_summary")
        return None
    if len(summary) < MIN_SUMMARY_CHARS:
        defects.append("summary_too_short")
        return None
    if len(summary) > MAX_SUMMARY_CHARS:
        defects.append("summary_too_long")
        return None
    if trajectory and len(trajectory) > MAX_TRAJECTORY_CHARS:
        defects.append("trajectory_too_long")
        return None
    person = facts.get("person") or ""
    if opens_with_name(summary, person):
        defects.append("opens_with_name")
        return None
    if dash_as_punctuation(summary) or (trajectory and dash_as_punctuation(trajectory)):
        defects.append("dash_punctuation")
        return None
    allowed = allowed_numbers(facts)
    for n in _INTEGER.findall(summary + " " + (trajectory or "")):
        if n not in allowed:
            defects.append(f"invented_number:{n}")
            return None
    # Rule 1: the newest stated role's title phrase must survive.
    roles = facts.get("roles") or []
    if roles:
        phrase = _norm(title_phrase(roles[0]["text"], person))
        if phrase and phrase not in _norm(summary):
            defects.append("stated_role_dropped")
            return None
    valid_ids = {r["id"] for r in roles} | {p["id"] for p in facts.get("perceptions", [])}
    if not isinstance(sources, list) or not sources:
        defects.append("no_sources")
        return None
    cited = [s for s in sources if isinstance(s, str) and s in valid_ids]
    if not cited:
        defects.append("unknown_sources")
        return None
    return {
        "summary": summary,
        "trajectory": trajectory,
        "sources": cited,
        "output_language": obj.get("output_language") if isinstance(obj, dict) else None,
    }


def served(card_value: Mapping[str, Any]) -> Dict[str, Any]:
    """The wire shape on person detail."""
    facts = card_value.get("facts") or {}
    by_id = {r["id"]: r for r in facts.get("roles", [])}
    by_id.update({p["id"]: p for p in facts.get("perceptions", [])})
    receipts = []
    for sid in card_value.get("sources") or []:
        src = by_id.get(sid)
        if not src:
            continue
        stated = sid.startswith("R")
        receipts.append({
            "kind": "stated" if stated else "observed",
            "text": src.get("text"),
            "origin_id": src.get("origin_id"),
            "date": src.get("date") or src.get("last_seen"),
            # A guarantee, not a convention (SS decodes it optional and
            # renders "seen N times" only when present): a stated role is
            # a patch with no confirmation count, so null; an observed
            # perception always carries its count, so an int.
            "times": None if stated else int(src.get("times") or 1),
        })
    return {
        "summary": card_value.get("text"),
        "trajectory": card_value.get("trajectory"),
        "receipts": receipts,
        "inputs_fingerprint": facts.get("fingerprint"),
        "generated_at": card_value.get("generated_at"),
        "model": card_value.get("model"),
    }


RETRYABLE = {"summary_too_long", "opens_with_name", "dash_punctuation", "stated_role_dropped"}


def retry_note(defect: str, facts: Mapping[str, Any], summary_chars: int = 0) -> Optional[str]:
    """A correction that CHANGES THE PROMPT, for one bounded retry. A
    blind repeat returns the same answer; telling the writer what was
    wrong is a different question. Only defects a rewrite can fix."""
    if defect == "summary_too_long":
        return (f"Your previous summary was {summary_chars} characters. The limit is "
                f"{MAX_SUMMARY_CHARS}. Rewrite it shorter: two sentences, no restating of the inputs.")
    if defect == "opens_with_name":
        return (f"Your previous summary opened with the person's name ({facts.get('person')}). "
                "It renders under their name, so do not begin with it; begin with the role or the read.")
    if defect == "dash_punctuation":
        return "Your previous answer used a dash as punctuation. Use a comma, a colon, or two sentences."
    if defect == "stated_role_dropped" and facts.get("roles"):
        phrase = title_phrase(facts["roles"][0]["text"], facts.get("person") or "")
        return (f"Your previous summary did not contain the newest stated role word for word. "
                f"It must include this phrase exactly: \"{phrase}\".")
    return None


def summary_chars(raw: Any) -> int:
    try:
        obj = raw if isinstance(raw, dict) else json.loads(raw)
        return len((obj.get("summary") or "").strip())
    except Exception:
        return 0
