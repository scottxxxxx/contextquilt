"""
JSON Schema for Context Quilt extraction output.

Used with providers that support structured-output / constrained decoding
(OpenAI, Gemini, DeepSeek, etc.) via the `json_schema` response_format.

The schema enforces:
- Top-level keys: patches, entities, relationships
- Patch type is one of the 10 registered V2 types
- Each patch has a value object (text + optional owner/deadline)
- Connection roles and entity types are enumerated

Providers that don't support json_schema fall back to json_object mode
and rely on prompt-described shape instead.
"""

PATCH_TYPES = [
    "trait",
    "preference",
    "identity",
    "role",
    "person",
    "project",
    "decision",
    "commitment",
    "blocker",
    "takeaway",
]

CONNECTION_ROLES = [
    "parent",
    "depends_on",
    "resolves",
    "replaces",
    "informs",
]

ENTITY_TYPES = [
    "person",
    "project",
    "company",
    "feature",
    "artifact",
    "deadline",
    "metric",
]

# Strict-mode-compatible schema. Every property is in `required`; optional
# semantic fields use nullable strings so the model can emit null when absent.
EXTRACTION_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    # Property order matters under OpenAI strict mode — the model generates
    # fields in the order they appear in `properties`. We exploit this to
    # force:
    #   1. The (you)-marker decision FIRST  (gating commitment)
    #   2. Reason-then-extract SECOND       (grounds patches in quotes)
    #   3. The patches array LAST           (committed to by the prior two)
    "required": ["you_speaker_present", "_reasoning", "patches", "entities", "relationships"],
    "properties": {
        "you_speaker_present": {
            "type": "boolean",
            "description": (
                "TRUE if any speaker label in the transcript contains the literal "
                "substring \"(you)\". FALSE otherwise. Set this first, before "
                "generating any patches. If FALSE, the patches array MUST NOT "
                "contain any patch of type trait, preference, or identity."
            ),
        },
        "_reasoning": {
            "type": "string",
            "description": (
                "Scratchpad for grounding patches in the transcript. Before "
                "emitting the patches array, list the 3-8 most load-bearing "
                "quotes from the transcript (verbatim, with their speaker "
                "label) and for each, state which patch type it supports and "
                "why. Keep under 400 words. This field is not persisted — it "
                "exists solely to force reason-then-extract ordering and to "
                "improve type classification (e.g., distinguishing 'prefers X "
                "over Y' as a preference, not a trait)."
            ),
        },
        "patches": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type", "value", "connects_to"],
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": PATCH_TYPES,
                    },
                    "value": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["text", "owner", "deadline"],
                        "properties": {
                            "text": {"type": "string"},
                            "owner": {"type": ["string", "null"]},
                            "deadline": {"type": ["string", "null"]},
                        },
                    },
                    "connects_to": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["target_text", "target_type", "role", "label"],
                            "properties": {
                                "target_text": {"type": "string"},
                                "target_type": {
                                    "type": "string",
                                    "enum": PATCH_TYPES,
                                },
                                "role": {
                                    "type": "string",
                                    "enum": CONNECTION_ROLES,
                                },
                                "label": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
        "entities": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "type", "description"],
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": ENTITY_TYPES},
                    "description": {"type": "string"},
                },
            },
        },
        "relationships": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["from", "to", "type", "context"],
                "properties": {
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                    "type": {"type": "string"},
                    "context": {"type": "string"},
                },
            },
        },
    },
}


def response_format() -> dict:
    """Return the `response_format` payload for a chat.completions call."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "cq_extraction",
            "schema": EXTRACTION_SCHEMA,
            "strict": True,
        },
    }


SELF_TYPED_PATCH_TYPES = frozenset({"trait", "preference", "identity"})


def strip_ephemeral_fields(content: dict) -> dict:
    """
    Remove fields that exist only to shape model output and are not meant
    to be persisted or returned to callers. Currently `_reasoning`.

    Call after enforce_owner_gate, before handing content to the
    downstream worker pipeline.
    """
    content.pop("_reasoning", None)
    return content


def sanitize_you_marker_from_patches(content: dict) -> dict:
    """
    Strip the literal '(you)' suffix from all patch text values.

    The '[Name (you)]' speaker label is a transcript-level identification
    marker that should never leak into stored patch text. Models sometimes
    copy it verbatim ('Scott (you) prefers async') despite prompt
    instructions not to. This function catches anything the prompt missed.

    Also strips from owner fields in case the model wrote 'Scott (you)'
    as the owner name.

    Call after enforce_owner_gate and enforce_connection_requirements,
    before storage.
    """
    for patch in content.get("patches") or []:
        value = patch.get("value")
        if not isinstance(value, dict):
            continue
        text = value.get("text", "")
        if "(you)" in text:
            value["text"] = (
                text.replace(" (you)", "").replace("(you) ", "").replace("(you)", "")
            )
        owner = value.get("owner", "")
        if owner and "(you)" in owner:
            value["owner"] = (
                owner.replace(" (you)", "").replace("(you) ", "").replace("(you)", "")
            )
    return content


# Types that only make sense attached to a project the (you) speaker owns.
# The quilt is user-centric — patches must anchor to something the user
# cares about. A decision/commitment/blocker/takeaway/role with no project
# parent is noise from the user's POV and gets dropped at ingest.
# Person patches are intentionally excluded: context about humans the user
# knows has standalone value even without project linkage.
PROJECT_SCOPED_TYPES = frozenset(
    {"decision", "commitment", "blocker", "takeaway", "role"}
)


def enforce_connection_requirements(content: dict) -> dict:
    """
    Drop project-scoped patches (decision/commitment/blocker/takeaway/role)
    that lack a valid parent connection to a project in the same extraction.

    Three drop reasons, logged in content["_connection_enforced"]:
      - no_parent_connection:      patch has no role="parent" entry
      - parent_target_not_project: parent connection points at wrong type
      - parent_target_not_in_output: parent target_text doesn't match any
                                     emitted project patch

    Call after enforce_owner_gate, before strip_ephemeral_fields.
    Mutates content in place; returns it for convenience.
    """
    patches = content.get("patches") or []
    project_texts = {
        (p.get("value") or {}).get("text", "")
        for p in patches
        if p.get("type") == "project"
    }
    project_texts.discard("")

    kept: list = []
    dropped: list = []
    for p in patches:
        ptype = p.get("type")
        if ptype not in PROJECT_SCOPED_TYPES:
            kept.append(p)
            continue

        parent_conns = [
            c for c in (p.get("connects_to") or []) if c.get("role") == "parent"
        ]

        if not parent_conns:
            reason = "no_parent_connection"
        elif all(c.get("target_type") != "project" for c in parent_conns):
            reason = "parent_target_not_project"
        elif not any(
            c.get("target_text") in project_texts
            for c in parent_conns
            if c.get("target_type") == "project"
        ):
            reason = "parent_target_not_in_output"
        else:
            kept.append(p)
            continue

        dropped.append(
            {
                "type": ptype,
                "text": (p.get("value") or {}).get("text", ""),
                "reason": reason,
            }
        )

    content["patches"] = kept
    if dropped:
        content["_connection_enforced"] = {
            "dropped": dropped,
            "count": len(dropped),
        }
    return content


def normalize_owner_in_transcript(
    transcript: str, owner_speaker_label: str | None
) -> str:
    """
    Ensure the transcript contains an inline `(you)` marker whenever the
    app has supplied an owner label. This is the platform-neutral bridge:
    apps that inject `(you)` themselves (e.g. SS's enrollment-time
    injection) pass through untouched; apps that only send a structured
    `owner_speaker_label` get the marker injected here so the downstream
    extraction pipeline has a single, consistent signal regardless of
    wire-format preference.

    Rules:
      - If `owner_speaker_label` is empty/None → no change.
      - If transcript already contains "(you)" → no change (app-injected).
      - Otherwise, replace every `[<label>]` with `[<label> (you)]`.

    Caveat: replacement is global. If two speakers share the owner's name
    (name-collision case), all occurrences get the marker. The correct
    long-term fix is per-turn ownership metadata; tracked as a deferred
    design item.
    """
    if not owner_speaker_label:
        return transcript
    if "(you)" in transcript:
        return transcript
    return transcript.replace(
        f"[{owner_speaker_label}]", f"[{owner_speaker_label} (you)]"
    )


def enforce_owner_gate(content: dict, transcript: str) -> dict:
    """
    Authoritatively enforce the owner-identity gating rule by filtering the
    model's response, independent of its self-reported flag.

    This is the platform-level gate: trait / preference / identity patches
    require a known owner. The only concrete signal the LLM sees is an
    inline `(you)` marker on a speaker label in the transcript it processes.
    Any app that wants self-typed extraction either injects that marker
    itself or sends `metadata.owner_speaker_label` for CQ to inject during
    normalization BEFORE the LLM call.

    By the time this function runs, the transcript either has the marker
    or doesn't. If it does → self-typed patches are allowed. If not →
    they're dropped regardless of what the model's `you_speaker_present`
    field claimed (observed: Mistral and GPT-4o-mini set this incorrectly).

    Mutates `content` in place and returns it.
    """
    if "(you)" in transcript:
        return content
    patches = content.get("patches") or []
    before = len(patches)
    content["patches"] = [
        p for p in patches if p.get("type") not in SELF_TYPED_PATCH_TYPES
    ]
    content["_owner_gate_enforced"] = {
        "marker_present": False,
        "filtered": before - len(content["patches"]),
    }
    return content


# Backwards-compat alias. Kept so existing imports don't break mid-stack;
# removable once downstream code standardizes on the new name.
enforce_you_marker_gate = enforce_owner_gate
