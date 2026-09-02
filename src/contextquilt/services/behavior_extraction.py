"""Behavior observations, extracted in their own call.

The `behavior` type shipped INLINE, as one of fifteen types in the main
extraction prompt, eleventh in the priority order, capped at five. On
eight real meetings (2026-08-15) that produced FOUR observations with
Haiku and ZERO with Sonnet. The same cheap model given a dedicated call
with the same task produced FORTY-EIGHT. Twelve times the yield, no
model change.

That is doc 19.5 exactly: prompt real estate is zero sum, and a type
competing with fourteen others for a model's attention loses. The rule's
own advice is to ask whether a new type deserves its own call, and this
one plainly does, because it is the corpus every person lens has been
starving for and it is the only type in the manifest that describes HOW
somebody behaved rather than WHAT happened.

The precedent for a second lightweight call at ingest is the
communication-profile call, which has run there since early on. The
transcript is available exactly once, at ingest, which is also why
speaker turn counts and question attribution are captured there
(doc 16 6.6).

WHAT THIS CALL MAY NOT DO. It writes ONE type. It does not extract
commitments, it does not name projects, and it does not get to decide
who the user is. The main extraction owns all of that, and a second
writer competing over the same ground is how two sources of truth start.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

# One meeting cannot support more than a handful of genuine observations
# about one person, and a model asked for more will pad. This is a
# per-CALL ceiling across everybody in the room; the manifest's own
# per-type cap still applies at the sink.
MAX_OBSERVATIONS = 12

# Below this there is no conduct to read: a two line status note has
# nothing in it about how anybody behaved, and asking anyway spends a
# call to be told so.
MIN_TRANSCRIPT_CHARS = 400

BEHAVIOR_SYSTEM = """You are the behavioral-observation stage of ContextQuilt, a persistent memory system. You are given one meeting transcript. Your ONLY job is to record how named participants CONDUCTED THEMSELVES in it.

You are not summarizing the meeting. You are not recording what was decided, what anybody committed to, or what is blocked. Another stage already does all of that and will do it better than you. If you find yourself writing down a task, you have drifted.

WHAT AN OBSERVATION IS. One thing a named person did, that a reader watching the meeting could have seen them do, phrased so it could be checked against the transcript.

Right shape:
- "Asked for the cost breakdown before agreeing to the vendor switch"
- "Moved off the launch date once the churn numbers were on screen"
- "Answered the compliance question by reading out the specific clause rather than summarizing it"
- "Went quiet after the budget was raised and did not return to it"
- "Volunteered to take the escalation before anyone assigned it"

Wrong shape, and why:
- "Is defensive about code review feedback" states what KIND of person somebody is. That is a verdict, not an observation, and it must never appear.
- "Will send the updated deck by Friday" is a commitment. Another stage owns it.
- "The team agreed to postpone" records what happened, not how anybody behaved.
- "Seems frustrated" is an interior state you cannot see. Record what they DID that a reader would read as frustration, or record nothing.

RULES:
- `owner` must be a named person exactly as the transcript names them. Never a role, never a diarization label like "Speaker 2", never "the team". If you cannot attribute conduct to a named person, do not record it.
- `owner` is the person who DID the thing, never the person it concerned. If the speaker marked "(you)" asked for two minutes to check an account, that is the user's conduct even when the account or the system belonged to somebody else, and it is not recorded.
- Never record an observation about the speaker marked "(you)". That is the user, and this corpus is about the people they work with.
- NEVER use a gendered pronoun for anyone (he, she, his, her, him, himself, herself). Gender is not observable in a transcript and a name or a voice does not state it. Use the person's name, or they, them, their.
- One observation per thing observed. Do not merge two moments into one sentence with "and also".
- Prefer the specific to the general. "Asked for last quarter's numbers before agreeing" beats "wanted more data".
- NEVER use a dash of any kind as punctuation. Use a comma, a colon, parentheses, or two sentences. A hyphen inside a genuinely hyphenated word is the only acceptable use.
- Record only what the transcript supports. A meeting where nobody did anything notable produces an empty list, and an empty list is a correct answer.

Respond with EXACTLY this raw JSON shape and nothing else:
{"observations": [{"text": "<what they did>", "owner": "<name exactly as the transcript names them>"}]}"""


def build_behavior_content(transcript: str, guidance: Optional[str] = None) -> str:
    """User content for one behavior call.

    `guidance` is the manifest's own wording for the type when an app
    declares it, so a per-app vocabulary reaches this call the same way
    it reaches the main extraction rather than being reinvented here.
    """
    parts = []
    if guidance:
        parts.append(f"App guidance for this type: {guidance}")
        parts.append("")
    parts.append("Transcript:")
    parts.append(transcript)
    return "\n".join(parts)


def parse_behavior_response(
    content: Any,
    user_label: Optional[str] = None,
    defects: Optional[List[str]] = None,
) -> List[Dict[str, str]]:
    """Observations as patch dicts ready for the normal sink, or [].

    Returns the SAME shape the main extraction emits for a patch, so the
    sanitizer chain and `store_connected_patches` handle these exactly
    as they handle everything else. That is the point: a second writer
    that also invented a second storage path would be two sources of
    truth, and the sink is where ownership edges, origin stamping, ACLs
    and dedup all live.

    Never raises. A malformed answer costs one call and no write.
    """
    obj = content
    if isinstance(obj, str):
        match = re.search(r"\{.*\}", obj, re.DOTALL)
        if not match:
            if defects is not None:
                defects.append("no_json")
            return []
        try:
            obj = json.loads(match.group())
        except json.JSONDecodeError:
            if defects is not None:
                defects.append("bad_json")
            return []
    if not isinstance(obj, dict):
        return []

    raw = obj.get("observations")
    if not isinstance(raw, list):
        return []

    self_forms = {(user_label or "").strip().lower(), "you", "(you)", "me"}
    self_forms.discard("")

    patches: List[Dict[str, str]] = []
    seen: set = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        owner = item.get("owner")
        if not isinstance(text, str) or not isinstance(owner, str):
            continue
        text = " ".join(text.split())
        owner = " ".join(owner.split())
        if not text or not owner:
            continue
        # The user is not a counterparty. The main extraction's own
        # sanitizers refuse a self person patch too; this stops the call
        # spending a slot on one rather than relying on that catch.
        if owner.strip().lower() in self_forms:
            continue
        key = (owner.strip().lower(), text.strip().lower())
        if key in seen:
            continue
        seen.add(key)
        patches.append({
            "type": "behavior",
            "value": {"text": text, "owner": owner},
        })
        if len(patches) >= MAX_OBSERVATIONS:
            break
    return patches


def worth_a_call(transcript: Optional[str]) -> bool:
    """Whether this transcript can support the question at all.

    A short status note has no conduct in it, and asking anyway spends a
    call to be told so. Cheap gate, checked before the model.
    """
    return bool(transcript) and len(transcript) >= MIN_TRANSCRIPT_CHARS
