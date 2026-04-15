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

    Call after enforce_you_marker_gate, before handing content to the
    downstream worker pipeline.
    """
    content.pop("_reasoning", None)
    return content


def enforce_you_marker_gate(content: dict, transcript: str) -> dict:
    """
    Authoritatively enforce the (you)-marker gating rule by filtering the
    model's response, independent of its self-reported flag.

    The extraction schema includes a `you_speaker_present` boolean the model
    is asked to set. Observed behavior: Claude Haiku honors it, Mistral and
    GPT-4o-mini do not. The transcript scan below is the ground-truth check.

    If no "(you)" substring is present in the transcript, any patch of type
    trait, preference, or identity is dropped. The content dict is mutated
    in place and returned for convenience.
    """
    if "(you)" in transcript:
        return content
    patches = content.get("patches") or []
    before = len(patches)
    content["patches"] = [
        p for p in patches if p.get("type") not in SELF_TYPED_PATCH_TYPES
    ]
    content["_you_gate_enforced"] = {
        "marker_present": False,
        "filtered": before - len(content["patches"]),
    }
    return content
