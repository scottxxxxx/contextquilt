"""Whether the user PARTICIPATED in this recording or merely heard it.

Doc 22. Scott read a podcast recording whose every patch was a
`behavior` and asked whether that was expected. It was: the main
extraction is built around participation (commitments with a named
owner, decisions that hold, blockers, projects the user owns) and a
listener has none of those, so the model declined and the only lane
that produced anything was the one asking how named people conducted
themselves. Measured across all callers on transcripts of 4,000
characters or more, unmarked calls yield zero patches 18.6% of the time
against 8.7% marked.

The worse harm was already in the data. `Leo` had 15
`person_appearances` rows at `speaker` capacity and `Paris Martineau` 6,
so CQ asserted the user had been in a room with podcast hosts, and doc
16 section 5.13 says a served name may assert only what was observed.

THE KIND CANNOT BE INFERRED. An absent `(you)` marker mislabels real
work meetings (73 of 142 substantial meetings arrive unmarked, and 5
substantial MARKED meetings still yielded nothing; Scott's own medical
appointment had a marker and produced zero). The app knows what the
user pressed record on and nothing downstream can recover it, which is
doc 19 rule 6: check what your instrument resolves through.

So it is DECLARED at capture, in `metadata.material_kind`, and ABSENT
MEANS `meeting`, which is today's behavior byte for byte. Nothing
changes for any existing caller.

WHAT `listening` SUPPRESSES, and why each one:
  - `commitment`: nobody owes the listener anything and an item in a
    ledger would be an obligation nobody made.
  - `behavior`: conduct of a stranger the listener will never meet.
  - person entities and appearances: presence means the user was in the
    room, and a recording is not a room.
  - `decision`, `project`: the listener decided nothing and owns
    nothing here.
  - the semantic role signals: they describe who assigned work to whom
    among participants.

WHAT IT KEEPS: `takeaway` (the evaluative lesson, which is what a
listener actually keeps), `event` (an external occurrence with context
implications), `artifact` (a named thing that exists and can be
opened). Intersected with what the caller's manifest declares, so an
app that never declared `artifact` does not start receiving one.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Set

MEETING = "meeting"
LISTENING = "listening"

#: Types honest for material the user did not participate in.
LISTENING_TYPES = ("takeaway", "event", "artifact")


def from_metadata(metadata: Optional[Mapping[str, Any]]) -> str:
    """The declared kind, or `meeting`.

    Anything unrecognised is `meeting` rather than an error: a client
    sending a kind CQ does not know yet must get today's behavior, not
    a dropped meeting. Open vocabulary in the additive direction only.
    """
    if not isinstance(metadata, Mapping):
        return MEETING
    raw = metadata.get("material_kind")
    if not isinstance(raw, str):
        return MEETING
    return LISTENING if raw.strip().lower() == LISTENING else MEETING


def is_listening(metadata: Optional[Mapping[str, Any]]) -> bool:
    return from_metadata(metadata) == LISTENING


#: What CQ knows how to resolve. Anything else is `meeting` by design,
#: and is worth SAYING so, which is what `unrecognised_kind` is for.
KNOWN_KINDS = (MEETING, LISTENING)


def unrecognised_kind(metadata: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The declared value, when one was sent and CQ does not know it.

    `None` when nothing was declared, because absent is not a mistake,
    and `None` when the value resolved. Present-and-unknown is the only
    case worth a line, and it is worth one precisely because it is the
    case that is INVISIBLE from the outcome: a typo and a flag that
    never arrived both extract as a meeting.

    GhostPour passes the value through untouched on purpose (their
    request-side proof, 2026-09-03: no trim, no lowercase, no default),
    which makes the two distinguishable ON THE WIRE. This makes them
    distinguishable in CQ's log too, which is where the doc 22
    acceptance test is actually read. Without it the next person to see
    a listening recording extract as a meeting would go hunting for a
    dropped field on a hop that forwarded it correctly.
    """
    if not isinstance(metadata, Mapping):
        return None
    raw = metadata.get("material_kind")
    if raw is None:
        return None
    if not isinstance(raw, str):
        return repr(raw)[:80]
    if raw.strip().lower() in KNOWN_KINDS:
        return None
    return raw


def allowed_types(manifest: Optional[Mapping[str, Any]]) -> Set[str]:
    """`LISTENING_TYPES` intersected with what the manifest declares.

    An app that never declared `artifact` does not start receiving one
    because this file has an opinion. With no manifest at all, the
    floor is the full set, which is what the legacy prompt path gets.
    """
    if not isinstance(manifest, Mapping):
        return set(LISTENING_TYPES)
    declared = {
        t.get("domain_type")
        for t in (manifest.get("patch_types") or [])
        if isinstance(t, dict)
    }
    if not declared:
        return set(LISTENING_TYPES)
    return {t for t in LISTENING_TYPES if t in declared}


def build_listening_system(allowed: Iterable[str]) -> str:
    """The extraction prompt for material the user only heard.

    The output shape is stated IN THE PROMPT because the Anthropic
    client accepts a `json_schema` for interface parity and does not put
    it on the wire (doc 19.3, and the edge-shape bug of 2026-09-01). An
    unstated field is an unemitted field.
    """
    names = [t for t in LISTENING_TYPES if t in set(allowed)]
    lines = {
        "takeaway": "- `takeaway`: an evaluative lesson worth remembering, with implications for the listener's own decisions. This is the type most of a recording becomes, and it is the one to prefer when you are unsure.",
        "event": "- `event`: something that HAPPENED in the world, with a date or period when one is stated. Not an opinion about it.",
        "artifact": "- `artifact`: a named thing that exists and could be opened or found later. A book, a paper, a product, a tool. Rare; most recordings name none.",
    }
    body = "\n".join(lines[n] for n in names)
    return (
        "You are the extraction stage of ContextQuilt, a persistent memory "
        "system. You are given a recording the user LISTENED TO. They were "
        "not in the room, they made no commitments, and the speakers are "
        "not their colleagues.\n\n"
        "Record what is worth keeping FOR THE LISTENER. Not a summary of "
        "the recording, and never a description of who said what.\n\n"
        f"THE ONLY TYPES YOU MAY EMIT:\n{body}\n\n"
        "NEVER emit a commitment, a decision, a blocker, a project, a "
        "person, an organisation or an observation about how a speaker "
        "conducted themselves. Nobody in this recording owes the listener "
        "anything, the listener decided nothing here, and a voice on a "
        "recording is not a colleague. If a claim only makes sense as "
        "something a speaker did, leave it out.\n\n"
        "Do not attribute a patch to anyone. There is no owner. Write what "
        "is true, not who said it.\n\n"
        "Say nothing rather than pad. A recording that taught the listener "
        "nothing produces an empty list, and an empty list is a correct "
        "answer.\n\n"
        "NEVER use a dash of any kind as punctuation. Use a comma, a colon, "
        "parentheses, or two sentences. A hyphen inside a genuinely "
        "hyphenated word is the only acceptable use.\n\n"
        "Respond with EXACTLY this raw JSON shape and nothing else:\n"
        '{"output_language": "<language code all output prose is written in>", '
        '"patches": [{"type": "<one of the types above>", '
        '"value": {"text": "<the fact, one clear sentence>"}}]}'
    )


def sanitize_listening_patches(
    content: Any, allowed: Iterable[str]
) -> Dict[str, Any]:
    """Keep only allowed types, strip owners and edges, drop the rest.

    The prompt says all of this and a model may ignore it, which is what
    put the behavior rules in code on 2026-09-01. `entities` and
    `relationships` are emptied here rather than at the call site, so
    the suppression travels with the shape rather than depending on a
    caller remembering it.

    Returns the content dict, mutated, with a report under
    `_listening_sanitized`.
    """
    if not isinstance(content, dict):
        return {"patches": [], "entities": [], "relationships": [],
                "_listening_sanitized": {"dropped": [], "count": 0}}
    allowed_set = {t for t in allowed}
    kept: List[dict] = []
    dropped: List[dict] = []
    for patch in content.get("patches") or []:
        if not isinstance(patch, dict):
            continue
        ptype = patch.get("type")
        value = patch.get("value")
        if isinstance(value, str):
            value = {"text": value}
        if not isinstance(value, dict) or not str(value.get("text") or "").strip():
            continue
        if ptype not in allowed_set:
            dropped.append({"type": ptype, "text": str(value.get("text"))[:120]})
            continue
        # No owner on anything here: an owner is a claim about a person,
        # and the people in a recording are not the listener's people.
        value.pop("owner", None)
        patch["value"] = value
        # No edges either. Every label in the vocabulary connects a patch
        # to a person, a project or a decision, and none of those exist
        # for this material.
        patch.pop("connects_to", None)
        kept.append(patch)
    content["patches"] = kept
    content["entities"] = []
    content["relationships"] = []
    content["resolved_commitments"] = []
    content["_listening_sanitized"] = {"dropped": dropped, "count": len(dropped)}
    return content
