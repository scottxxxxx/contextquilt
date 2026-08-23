"""The Alignment Layer, phase 1: the record.

Design e6ee7ae8 ("Alignment in the App" + "Alignment Layer -
Requirements"), directed by Scott 2026-08-23. One object, the alignment
event, authored ONCE by CQ after a meeting and rendered three ways by
the app: confirmed in the meeting view, aggregated on the project, and
rehearsed before the next meeting.

THE RULE (requirements section 3, non-negotiable): private intelligence
never becomes a shared label. Shared text says what the project
currently believes, how that changed, and what it cost. It never
references a person's tendencies, and the sequence is never annotated,
because four dated events in a row IS the argument and a label turns a
record into an accusation. That boundary is enforced in code here, not
by the prompt: `guard_shared_text` rejects, and the worker regenerates
or drops; nothing is softened.

The four steps the requirements assign to CQ, and where each lives:

  1. DETECT, supersede not sentiment. The model is handed this meeting's
     decision patches and the project's ACTIVE decision set (prior
     alignment events + prior decision patches) and names, BY ID, which
     active items a new decision supersedes (resolved_commitments
     pattern; doc 19.1, the model may identify but may not count).
     Sentiment and speaker attributes play no part: the prompt never
     sees who said what beyond the owner string the patches carry.
  2. SCOPE, attach the cost. `derive_impact` is computed in code from the
     open items that reference the superseded decisions. Never free
     text. It says what is dated, what is overdue and who owns it; it
     does not invent dev-days (requirements 11, open question, left
     qualitative on purpose until Scott rules).
  3. PHRASE, facts only. The statement and rationale come from the model
     and must pass `guard_shared_text`.
  4. ROUTE, two audiences. Shared text is what the routes serve. The
     private instruction is built in code (`private_instruction`), with
     the count computed by code, and stored in a column no shared read
     selects.

Evidence is mandatory: an event whose quote cannot be found in the
transcript is stored with `shippable=False` and never reaches a shared
surface (requirements 4). Low confidence phrases as a question
(requirements 6); the model is told to, and `shippable` still requires
evidence.

Everything in this module is pure and unit-tested; the DB and LLM calls
live in the worker and the routes.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

from contextquilt.services.follow_through import (
    CHARACTER_TRAIT_WORDS,
    CHARACTER_WORDS,
    character_word_in,
)

PROPOSAL_TTL_HOURS = 72

CONFIDENCE_LEVELS = ("high", "moderate", "emerging")
STATUSES = ("proposed", "confirmed", "corrected", "expired")

# Shared-surface denylist, on top of the character words the behavior
# guardrail already refuses. These are the terms that turn a project fact
# into a claim about a person's tendencies (requirements 3: no
# "inconsistency", no "reversal", no counts framed as a person's score).
# Word-bounded, case-insensitive. A match REJECTS; nothing is softened.
TENDENCY_TERMS = (
    "inconsistent", "inconsistency", "inconsistencies",
    "reversal", "reversals", "flip-flop", "flip-flopped", "flip-flopping",
    "indecisive", "indecision", "waffling", "wavering",
    "changed (?:his|her|their) mind", "change(?:s|d)? (?:his|her|their) mind",
    "keeps? changing", "kept changing", "keeps? (?:moving|shifting)",
    "tends? to", "tendency", "habit", "habitually", "as usual",
    "yet again", "once again", "again", "repeatedly", "pattern of",
    "third time", "second time", "fourth time", "nth time",
    "unreliable", "erratic", "unpredictable",
)
_TENDENCY_RE = re.compile(
    r"\b(?:" + "|".join(TENDENCY_TERMS) + r")\b", re.IGNORECASE
)


def guard_shared_text(text: Optional[str]) -> Optional[str]:
    """The first reason a shared-surface string is rejected, or None.

    Returns the offending term so the worker can log WHAT tripped it and
    the regeneration prompt can name it. Empty text passes (a null
    rationale is allowed); the caller decides whether a field is
    required.
    """
    if not text:
        return None
    word = character_word_in(text, CHARACTER_WORDS + CHARACTER_TRAIT_WORDS)
    if word:
        return word
    m = _TENDENCY_RE.search(text)
    if m:
        return m.group(0)
    return None


# ---------------------------------------------------------------------
# Step 1: the prompt. Deltas, not history (requirements 5): compact
# active decision state plus this meeting's decisions and the transcript.
# ---------------------------------------------------------------------

ALIGNMENT_SYSTEM = (
    "You maintain a project's shared alignment record. You are given the "
    "project's ACTIVE decision set (what the project currently believes, "
    "each with an id) and the decisions recorded from today's meeting "
    "(each with an id), plus the transcript.\n\n"
    "Your only job is to find SUPERSESSION: a decision from today that is "
    "about the same topic as an active item but reaches a different "
    "outcome. Same topic, same outcome is a reconfirmation, not a change; "
    "a new topic with no active counterpart is not a change. Tone, mood, "
    "sentiment and who said what play no part.\n\n"
    "For each change you find, write the shared record entry. Shared text "
    "states what the project now believes, what it replaces, why (if the "
    "transcript says why), and who owns the decision. It never describes "
    "a person: no tendencies, no counts of how often something changed, "
    "no words like inconsistent, reversal, again, keeps changing, or any "
    "judgement of anyone. Write one plain sentence for the statement. "
    "If you are not confident the change happened, set confidence to "
    "\"emerging\" and phrase the statement as a question.\n\n"
    "Quote the evidence VERBATIM from the transcript: copy the exact words, "
    "at least eight of them, from one turn. Do not paraphrase the quote.\n\n"
    "Output ONLY raw JSON, no prose, no code fence, exactly this shape:\n"
    "{\"events\": [{\"new_decision_id\": \"<id from today's list>\", "
    "\"supersedes_ids\": [\"<id from the active list>\"], "
    "\"topic\": \"<two to four lowercase words, hyphenated>\", "
    "\"statement\": \"<one sentence>\", \"rationale\": \"<one sentence or null>\", "
    "\"decision_owner\": \"<name or null>\", \"implementation_owner\": \"<name or null>\", "
    "\"evidence_quote\": \"<verbatim words from the transcript>\", "
    "\"confidence\": \"high\" | \"moderate\" | \"emerging\"}]}\n"
    "If nothing was superseded, output {\"events\": []}."
)


def build_alignment_content(
    meeting_date: str,
    meeting_decisions: list[dict],
    active_set: list[dict],
    transcript: str,
    transcript_cap: int = 24000,
) -> str:
    """Render the user turn. `meeting_decisions` and `active_set` are
    [{id, text, owner?, date?}]. The transcript is capped from the END
    (decisions tend to land late) but the cap is stated so the model does
    not mistake a cut for silence."""
    lines = [f"Meeting date: {meeting_date}", "", "ACTIVE DECISION SET (id: text):"]
    if active_set:
        for a in active_set:
            when = f" [{a['date']}]" if a.get("date") else ""
            lines.append(f"- {a['id']}: {a['text']}{when}")
    else:
        lines.append("- (none recorded yet)")
    lines += ["", "TODAY'S DECISIONS (id: text):"]
    for d in meeting_decisions:
        owner = f" (owner: {d['owner']})" if d.get("owner") else ""
        lines.append(f"- {d['id']}: {d['text']}{owner}")
    body = transcript or ""
    if len(body) > transcript_cap:
        body = "[...earlier transcript omitted...]\n" + body[-transcript_cap:]
    lines += ["", "TRANSCRIPT:", body]
    return "\n".join(lines)


# ---------------------------------------------------------------------
# Parse. Ids are validated against the lists the model was handed; a
# hallucinated id is dropped, never guessed. Evidence is checked against
# the transcript; a quote that is not there makes the event unshippable.
# ---------------------------------------------------------------------

def _norm(s: str) -> str:
    # Apostrophes are removed, not split on, so "Srikanthi's" and a
    # model's "Srikanthis" fold to the same token; everything else that
    # is not a letter or digit becomes a single space.
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower().replace("'", "").replace("\u2019", "")).strip()


def evidence_in_transcript(quote: Optional[str], transcript: str, min_words: int = 6) -> bool:
    """True when the quote's words appear contiguously in the transcript
    after punctuation and case are folded. Short quotes are refused: six
    words is the floor below which a match proves nothing."""
    q = _norm(quote or "")
    if len(q.split()) < min_words:
        return False
    return q in _norm(transcript or "")


def parse_alignment_response(
    content: Any,
    meeting_decision_ids: Iterable[str],
    active_ids: Iterable[str],
    transcript: str,
    defects: Optional[list] = None,
) -> list[dict]:
    """Validated candidate events. Each carries `guard_hit` (None or the
    term that tripped the guard) and `shippable` (evidence found AND no
    guard hit); the worker decides whether to regenerate or store as a
    private candidate. Never raises."""
    defects = defects if defects is not None else []
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except Exception:
            defects.append("not_json")
            return []
    if not isinstance(content, dict):
        defects.append("not_object")
        return []
    events = content.get("events")
    if not isinstance(events, list):
        defects.append("no_events_list")
        return []
    new_ids = {str(i) for i in meeting_decision_ids}
    act_ids = {str(i) for i in active_ids}
    out: list[dict] = []
    for e in events:
        if not isinstance(e, dict):
            continue
        nid = str(e.get("new_decision_id") or "")
        if nid not in new_ids:
            defects.append(f"unknown_new_id:{nid[:12]}")
            continue
        sup = [str(s) for s in (e.get("supersedes_ids") or []) if str(s) in act_ids]
        if not sup:
            # Same-topic-different-outcome needs a counterpart; a change
            # that supersedes nothing the project holds is a new decision,
            # which the ordinary extraction already recorded.
            defects.append("supersedes_nothing")
            continue
        statement = (e.get("statement") or "").strip()
        if not statement:
            defects.append("empty_statement")
            continue
        rationale = (e.get("rationale") or None)
        rationale = rationale.strip() if isinstance(rationale, str) and rationale.strip() else None
        topic = re.sub(r"[^a-z0-9]+", "-", str(e.get("topic") or "").lower()).strip("-") or "untitled"
        confidence = e.get("confidence") if e.get("confidence") in CONFIDENCE_LEVELS else "moderate"
        quote = (e.get("evidence_quote") or "").strip()
        has_evidence = evidence_in_transcript(quote, transcript)
        guard_hit = guard_shared_text(statement) or guard_shared_text(rationale)
        out.append({
            "new_decision_id": nid,
            "supersedes_ids": sup,
            "topic": topic[:80],
            "statement": statement[:400],
            "rationale": rationale[:400] if rationale else None,
            "decision_owner": (e.get("decision_owner") or None),
            "implementation_owner": (e.get("implementation_owner") or None),
            "evidence_quote": quote if has_evidence else None,
            "confidence": confidence,
            "guard_hit": guard_hit,
            "shippable": bool(has_evidence and not guard_hit),
        })
    return out


# ---------------------------------------------------------------------
# Step 2: the impact receipt, derived in code.
# ---------------------------------------------------------------------

def derive_impact(referencing_items: list[dict], today_iso: str) -> list[dict]:
    """ImpactLine[] from the open items that reference the superseded
    decisions. `referencing_items` = [{patch_id, patch_type, text, owner,
    deadline_date, overdue}] resolved by the worker through connections
    and shared cues. The effect is a FACT about the item (its date and
    state), never an estimate; magnitude drives the bar only and is a
    fixed ladder, not a ratio (doc 16 rules: no ratio at any denominator).
    """
    lines: list[dict] = []
    for it in referencing_items:
        deadline = it.get("deadline_date")
        if it.get("overdue"):
            effect, mag = f"Overdue since {deadline}" if deadline else "Overdue", 1.0
        elif deadline:
            effect, mag = ("Due today" if deadline == today_iso else f"Due {deadline}"), 0.6
        else:
            effect, mag = "Open, no date", 0.3
        subject = (it.get("text") or "").strip()
        if len(subject) > 80:
            subject = subject[:77].rstrip() + "..."
        lines.append({
            "subject": subject,
            "effect": effect,
            "magnitude": mag,
            "owner": it.get("owner") or None,
            "item_type": it.get("patch_type"),
            "derived_from": [str(it.get("patch_id"))],
        })
    # Overdue first, then dated, then undated; stable within a band so a
    # browse surface never reshuffles between polls.
    lines.sort(key=lambda l: -l["magnitude"])
    return lines


# ---------------------------------------------------------------------
# Step 4: the private half. Built in code; the count is code's.
# ---------------------------------------------------------------------

def private_instruction(topic: str, change_count: int) -> str:
    """What the reader should do, never who anyone is. Served only on a
    private route (phase 3); stored now so the count is right later."""
    times = {1: "once", 2: "twice", 3: "three times", 4: "four times"}.get(
        change_count, f"{change_count} times"
    )
    human_topic = topic.replace("-", " ")
    return (
        f"Direction on {human_topic} has changed {times}. Capture the "
        "decision, owner, scope, and confirmation date in the record "
        "before work restarts."
    )


def topic_change_count(events: list[dict], topic: str) -> int:
    """How many events on this topic superseded something. Computed from
    stored rows, which is the only place a count is allowed to come from."""
    return sum(
        1 for e in events
        if e.get("topic") == topic and (e.get("supersedes") or []) and e.get("status") != "expired"
    )


# ---------------------------------------------------------------------
# The project record (phase 2 read, cheap enough to serve from day one
# because the phase 1 card deep-links to it).
# ---------------------------------------------------------------------

def project_record(events: list[dict]) -> dict:
    """Current direction per topic, awaiting list, history, change count
    and cumulative impact, from stored rows only. `events` are dicts with
    the column names of alignment_events, shared columns only; the
    caller must never pass private_instruction in."""
    by_id = {str(e["event_id"]): e for e in events}
    topics: dict[str, list[dict]] = {}
    for e in events:
        topics.setdefault(e["topic"], []).append(e)

    current: list[dict] = []
    awaiting: list[dict] = []
    for topic, rows in topics.items():
        rows.sort(key=lambda r: r["proposed_at"])
        confirmed = [r for r in rows if r["status"] == "confirmed" and not r.get("superseded_by")]
        if confirmed:
            current.append(confirmed[-1])
        for r in rows:
            if r["status"] in ("proposed", "corrected") and not r.get("confirmed_at") \
                    and not r.get("superseded_by"):
                awaiting.append(r)

    history = sorted(events, key=lambda r: r["proposed_at"])
    change_count = sum(
        1 for e in events if e["status"] == "confirmed" and (e.get("supersedes") or [])
    )
    cumulative: list[dict] = []
    seen: set = set()
    for e in events:
        if e["status"] != "confirmed" or not (e.get("supersedes") or []):
            continue
        for line in e.get("impact") or []:
            key = tuple(line.get("derived_from") or [])
            if key in seen:
                continue
            seen.add(key)
            cumulative.append(line)
    return {
        "current_directions": current,
        "awaiting_confirmation": sorted(awaiting, key=lambda r: r.get("expires_at") or ""),
        "history": history,
        "direction_change_count": change_count,
        "cumulative_impact": cumulative,
        "by_id": by_id,
    }
