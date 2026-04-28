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
    {
        "decision", "commitment", "blocker", "takeaway", "role",
        "goal", "constraint", "event", "deliverable",
    }
)

# Subset of project-scoped types that should prefer a `deliverable` as
# their auto-parent target when one is unambiguously present in the
# same extraction output. These are the "what happened / what needs to
# happen" episodes that naturally hang off a specific deliverable. The
# remaining project-scoped types (goal, constraint, role, deliverable
# itself) stay parented to the top-level project — goals and constraints
# are usually engagement-wide, and deliverable/role parent to project by
# definition.
DELIVERABLE_CHILD_TYPES = frozenset(
    {"decision", "commitment", "blocker", "takeaway", "event"}
)

# Valid parent target types (mirrors manifest belongs_to.to_types).
# `deliverable` joined `project` as a valid parent target in v1.1 so
# children of a deliverable get grouped under it rather than flattened
# under the top-level project.
VALID_PARENT_TARGET_TYPES = frozenset({"project", "deliverable"})


def enforce_connection_requirements(
    content: dict, meeting_project: str | None = None
) -> dict:
    """
    Ensure project-scoped patches have a valid parent connection.

    When `meeting_project` is supplied (the project context the extraction
    is running under), patches missing a parent connection — or pointing at
    a target name absent from the current output — get a synthetic parent
    connection injected instead of being dropped. The Pass-2 connection
    resolver in `store_connected_patches` already matches targets against
    existing DB patches, so the injected edge resolves to the pre-existing
    project or deliverable row.

    Auto-parent target selection (v1.1):
      - If exactly ONE `deliverable` patch is present in the same output
        AND the orphan patch type is in DELIVERABLE_CHILD_TYPES, parent
        to the deliverable (narrowest valid parent).
      - Otherwise parent to `meeting_project`.

    Dropped only when the source is genuinely malformed:
      - parent_target_invalid: parent points at a target type that isn't
        `project` or `deliverable`.

    When no `meeting_project` is supplied (e.g. trace/conversation paths),
    behavior falls back to the strict pre-injection rule set:
      - no_parent_connection
      - parent_target_invalid
      - parent_target_not_in_output

    Audit detail is recorded in content["_connection_enforced"]:
      {
        "dropped":     [...patches dropped for structural violations...],
        "count":       total dropped,
        "auto_parented": [...patches given a synthetic parent connection...],
      }

    Call after enforce_owner_gate, before strip_ephemeral_fields. Mutates
    content in place; returns it for convenience.
    """
    patches = content.get("patches") or []
    project_texts = {
        (p.get("value") or {}).get("text", "")
        for p in patches
        if p.get("type") == "project"
    }
    project_texts.discard("")

    deliverable_texts = [
        (p.get("value") or {}).get("text", "")
        for p in patches
        if p.get("type") == "deliverable"
    ]
    deliverable_texts = [t for t in deliverable_texts if t]
    # Prefer deliverable as parent only when exactly one is in scope —
    # ambiguity with multiple deliverables defeats the safety net.
    preferred_deliverable = (
        deliverable_texts[0] if len(deliverable_texts) == 1 else None
    )

    valid_parent_texts = project_texts | set(deliverable_texts)

    kept: list = []
    dropped: list = []
    auto_parented: list = []

    def _inject_parent(patch: dict) -> None:
        ptype = patch.get("type")
        if (
            preferred_deliverable is not None
            and ptype in DELIVERABLE_CHILD_TYPES
        ):
            target_text = preferred_deliverable
            target_type = "deliverable"
        else:
            target_text = meeting_project
            target_type = "project"
        patch.setdefault("connects_to", []).append(
            {
                "role": "parent",
                "label": "belongs_to",
                "target_type": target_type,
                "target_text": target_text,
            }
        )
        auto_parented.append(
            {
                "type": ptype,
                "text": (patch.get("value") or {}).get("text", ""),
                "parent_type": target_type,
                "parent_text": target_text,
            }
        )

    for p in patches:
        ptype = p.get("type")
        if ptype not in PROJECT_SCOPED_TYPES:
            kept.append(p)
            continue

        parent_conns = [
            c for c in (p.get("connects_to") or []) if c.get("role") == "parent"
        ]

        if not parent_conns:
            if meeting_project:
                _inject_parent(p)
                kept.append(p)
                continue
            reason = "no_parent_connection"
        elif all(
            c.get("target_type") not in VALID_PARENT_TARGET_TYPES
            for c in parent_conns
        ):
            reason = "parent_target_invalid"
        elif not any(
            c.get("target_text") in valid_parent_texts
            for c in parent_conns
            if c.get("target_type") in VALID_PARENT_TARGET_TYPES
        ):
            if meeting_project:
                # LLM wired a parent but pointed at a project/deliverable
                # name not emitted in this output. If `meeting_project` is
                # in scope, trust the Pass-2 resolver to match against
                # existing DB rows — no synthetic injection needed.
                kept.append(p)
                continue
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
    if dropped or auto_parented:
        content["_connection_enforced"] = {
            "dropped": dropped,
            "count": len(dropped),
            "auto_parented": auto_parented,
        }
    return content


# Action-item types that gain a `person → owns → action` connection when
# the LLM extracts a named human as their owner. Mirrors the SS app's
# `owns` connection vocabulary: from={person} to={commitment,blocker,decision,goal}.
PERSON_OWNED_ACTION_TYPES = frozenset(
    {"commitment", "blocker", "decision", "goal"}
)

# Owner-text values that MUST NOT trigger a synthetic person patch:
# - the (you) speaker (their attribution is implicit via patch ownership)
# - diarization placeholders that aren't real human names
# - empty / unknown markers
_OWNER_PLACEHOLDER_PREFIXES = ("speaker ", "speaker_", "unknown", "unidentified")
_OWNER_YOU_TOKENS = frozenset({"(you)", "you", "self", "me", "i"})


def _is_real_person_owner(owner_text: str | None, user_label: str | None) -> bool:
    """Return True iff `owner_text` looks like a real named human (not the
    submitting user, not a diarization placeholder).

    Used by enforce_person_ownership to decide whether an action-item
    patch's owner warrants a synthetic person patch.
    """
    if not owner_text:
        return False
    s = owner_text.strip()
    if not s:
        return False
    low = s.lower()
    if low in _OWNER_YOU_TOKENS:
        return False
    if any(low.startswith(p) for p in _OWNER_PLACEHOLDER_PREFIXES):
        return False
    if user_label and low == user_label.strip().lower():
        # The (you) speaker. Their ownership is implicit.
        return False
    return True


def enforce_person_ownership(
    content: dict, user_label: str | None = None
) -> dict:
    """
    Ensure every action-item patch with a named human owner has a
    corresponding `person` patch + `owns` connection in the output.

    The prompt already requires this (extraction_prompts.py: "Every person
    who owns a commitment, blocker, or decision MUST be a person patch —
    not just an entity"), but real-world Haiku 4.5 compliance is unreliable.
    Action items routinely come back with `value.owner: "Brian"` and no
    Brian person patch and no `owns` connection. This is the structural
    safety net — same shape as enforce_connection_requirements for parents.

    For each commitment/blocker/decision/goal in content["patches"]:
      1. Read value.owner. Skip if empty, the (you) speaker, or a
         diarization placeholder ("Speaker N", "Unknown").
      2. Find an existing person patch in patches[] whose value.text
         matches the owner name (case-insensitive).
      3. If absent, inject a synthetic person patch.
      4. Ensure a `person → action_item` `owns` connection exists. If
         absent, append one to the person patch's connects_to.

    Audit detail recorded in content["_person_ownership_enforced"]:
      {
        "persons_injected":     [...names of person patches added...],
        "connections_injected": [...{owner, target_text, target_type}...],
      }

    Idempotent: running twice on the same content does nothing the second
    time.

    Call after enforce_connection_requirements, before
    strip_ephemeral_fields. Mutates content in place; returns it for
    convenience.
    """
    patches = content.get("patches") or []
    if not patches:
        return content

    # Fast lookup of existing person patches by lowercased text.
    person_index: dict[str, dict] = {}
    for p in patches:
        if p.get("type") != "person":
            continue
        text = ((p.get("value") or {}).get("text") or "").strip()
        if text:
            person_index[text.lower()] = p

    persons_injected: list[str] = []
    connections_injected: list[dict] = []

    def _ensure_person(name: str) -> dict:
        """Return the person patch for `name`, creating it if absent."""
        key = name.strip().lower()
        existing = person_index.get(key)
        if existing is not None:
            return existing
        synthetic = {
            "type": "person",
            "value": {"text": name.strip()},
            "connects_to": [],
        }
        patches.append(synthetic)
        person_index[key] = synthetic
        persons_injected.append(name.strip())
        return synthetic

    def _ensure_owns_edge(person: dict, target_text: str, target_type: str) -> None:
        """Append a person → action `owns` edge if not already present."""
        edges = person.setdefault("connects_to", [])
        for c in edges:
            if (
                c.get("label") == "owns"
                and c.get("target_type") == target_type
                and (c.get("target_text") or "").strip().lower()
                    == target_text.strip().lower()
            ):
                return
        edges.append(
            {
                "role": "informs",
                "label": "owns",
                "target_type": target_type,
                "target_text": target_text,
            }
        )
        connections_injected.append(
            {
                "owner": (person.get("value") or {}).get("text", ""),
                "target_text": target_text,
                "target_type": target_type,
            }
        )

    # Snapshot the patches list before we start mutating it — we only
    # iterate the action items present at entry, not any synthetic person
    # patches we append below.
    action_items = [
        p for p in list(patches) if p.get("type") in PERSON_OWNED_ACTION_TYPES
    ]
    for p in action_items:
        owner = (p.get("value") or {}).get("owner")
        if not _is_real_person_owner(owner, user_label):
            continue
        target_text = (p.get("value") or {}).get("text") or ""
        if not target_text:
            continue
        person = _ensure_person(owner)
        _ensure_owns_edge(person, target_text, p.get("type"))

    if persons_injected or connections_injected:
        content["_person_ownership_enforced"] = {
            "persons_injected": persons_injected,
            "connections_injected": connections_injected,
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
