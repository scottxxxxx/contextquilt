"""Live speaker labels, answered on the device, applied at ingest.

THE GAP THIS CLOSES (2026-08-23, Scott's first device test of the
contested-name picker). Labelling a speaker during a LIVE recording is
the primary path and it never calls reassign-speaker: the typed name
rides inside the capture POST as the bracketed label in the transcript.
Ingest then resolves a bare "christina" through step 2 (any recorded
alias, LIMIT 1, any source) or step 3 (the unique-candidate heuristic,
which WRITES a heuristic alias), silently. The contested guard at 1b
fires only when two people could match, and after a silent absorption
there is never a second, so the two-Johns shape was reachable from the
most common labelling path with no signal anywhere.

The client asks the question live from its cached roster (the one
place it builds the candidate list, because there is no server meeting
to ask yet), records the answer per label, and sends it on the capture
as `metadata.speaker_identities`. CQ applies it HERE, before extraction,
by REWRITING the bracketed label to the chosen person's stored canonical
name. That is deliberately a rewrite rather than a second resolution
path: after it, the canonical name is what the model reads, what
store_entities hits on its exact match (step 1, before any alias or
heuristic), what `value.owner` carries, what the appearance row is
written for and what turn counts key on. One mechanism, nothing
downstream changes, and the server never holds the bare label at all,
so the client's local map and CQ agree by construction. Same shape as
the owner-marker precedent (`normalize_owner_in_transcript`).

Wire shape, inside `metadata`:
    "speaker_identities": [
      {"label": "christina", "entity_id": "<uuid>"},
      {"label": "Speaker 2", "create_new": true, "name": "Christina Lopez"}
    ]
An entry with both uses entity_id. Malformed entries are dropped, never
fatal. A label with no entry, an unknown entity_id or any failure is left
untouched and today's matching applies: a bad map never loses a meeting.

The DB half (load the person, or create through the same rules as
reassign-speaker's create_new including the Keep separate stamp) lives
in the worker; everything here is pure and unit-tested.
"""
from __future__ import annotations

import re
from typing import Iterable

# Speaker labels are `[Label]` or `[Label (you)]`; the owner marker is
# injected before this runs and must survive the rewrite.
_YOU_SUFFIX = r"(\s*\(you\))?"


def parse_speaker_identities(raw: object) -> list[dict]:
    """Validate the wire list. Returns only well-formed entries, each as
    {"label": str, "entity_id": str | None, "name": str | None,
    "create_new": bool}. Never raises."""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen_labels: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = item.get("label")
        if not isinstance(label, str) or not label.strip():
            continue
        key = label.strip().lower()
        if key in seen_labels:
            continue  # first answer for a label wins; a map is not a log
        entity_id = item.get("entity_id")
        entity_id = entity_id.strip() if isinstance(entity_id, str) and entity_id.strip() else None
        name = item.get("name")
        name = name.strip() if isinstance(name, str) and name.strip() else None
        create_new = bool(item.get("create_new")) and entity_id is None
        if entity_id is None and not (create_new and name):
            continue
        seen_labels.add(key)
        out.append({
            "label": label.strip(),
            "entity_id": entity_id,
            "name": name if create_new else None,
            "create_new": create_new,
        })
    return out


def rewrite_speaker_labels(transcript: str, mapping: dict[str, str]) -> tuple[str, dict[str, int]]:
    """Replace every `[label]` / `[label (you)]` with the canonical name,
    case-insensitively on the label. Returns the new text and a count of
    replacements per label (so the caller can log a label that matched
    nothing, which is the client sending a key the transcript does not
    use)."""
    if not transcript or not mapping:
        return transcript, {}
    counts: dict[str, int] = {}
    text = transcript
    for label, canonical in mapping.items():
        if not label or not canonical or label.lower() == canonical.lower():
            counts[label] = 0
            continue
        pattern = re.compile(r"\[" + re.escape(label) + _YOU_SUFFIX + r"\]", re.IGNORECASE)
        text, n = pattern.subn(lambda m: f"[{canonical}{m.group(1) or ''}]", text)
        counts[label] = n
    return text, counts


def labels_in_transcript(transcript: str) -> set[str]:
    """Lower-cased speaker labels present in the text, `(you)` stripped."""
    if not transcript:
        return set()
    found = re.findall(r"\[([^\]]{1,60})\]", transcript)
    out = set()
    for f in found:
        f = re.sub(r"\s*\(you\)\s*$", "", f, flags=re.IGNORECASE).strip().lower()
        if f:
            out.add(f)
    return out
