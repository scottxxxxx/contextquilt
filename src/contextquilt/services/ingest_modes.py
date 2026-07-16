"""
Ingest-mode routing rules (the "transformer" contract).

A manifest's `ingest_mode` declares which ingest surface the app uses —
which adapter its payloads are allowed to reach:

  extraction — transcript/prose input; the LLM extraction adapter
               (handle_meeting_summary and friends) turns it into patches.
  structured — pre-typed patches; the structured adapter
               (handle_structured_ingest) validates and stores them.

The worker enforces this ONLY when the manifest explicitly declares
ingest_mode. An absent manifest, or a manifest without the key, gets
legacy routing (client-declared interaction_type, unrestricted) — the
ShoulderSurf manifest predates ingest_mode and must stay byte-identical.

Why enforce at all: without the gate, a structured-mode app that sends a
transcript-shaped payload silently flows through LLM extraction with a
generic prompt — plausible-looking garbage lands in the quilt with no
error anywhere. Declared mode turns that into a logged rejection.
"""

from __future__ import annotations

from typing import Optional

# interaction_types owned by each adapter. Types outside the union
# (system tasks like hydrate/tool_call, unknown types) are not gated.
INGEST_MODE_TYPES = {
    "extraction": {
        "meeting_transcript", "meeting_summary", "summary",
        "query", "analysis", "sentiment", "trace", "chat_log",
        # Contract item 9: prose corrections ride the extraction adapter.
        "correction",
        # Contract item 10: chat completions close open completables.
        "completion",
    },
    "structured": {"structured_patches"},
}

_ALL_GATED_TYPES = frozenset().union(*INGEST_MODE_TYPES.values())


def is_interaction_allowed(ingest_mode: Optional[str], task_type: Optional[str]) -> bool:
    """Whether a task of `task_type` may proceed for an app whose manifest
    declares `ingest_mode`.

    - No declared mode (None) → allowed (legacy routing).
    - Unknown/system task types → allowed (not this gate's job).
    - Declared mode → task_type must belong to that mode's adapter.
    """
    if not ingest_mode or ingest_mode not in INGEST_MODE_TYPES:
        return True
    if task_type not in _ALL_GATED_TYPES:
        return True
    return task_type in INGEST_MODE_TYPES[ingest_mode]
