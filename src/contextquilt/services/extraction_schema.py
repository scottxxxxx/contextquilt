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

import re
from datetime import date, timedelta

from .follow_through import (
    CHARACTER_TRAIT_WORDS,
    CHARACTER_WORDS,
    character_word_in,
)
# The appearance-capacity vocabulary lives in one module so the sanitizer,
# the worker's sink and the backfill cannot drift apart on what a capacity
# is called.
from .person_appearances import MENTION, OWNERSHIP

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
    #   1. The (you)-marker decision FIRST    (gating commitment)
    #   2. output_language SECOND             (language commitment — anchors all
    #      downstream prose; without it, English context injected into
    #      user_content (e.g. the open-commitments block) pulls patch text
    #      back to English even when the user's language is Spanish)
    #   3. Reason-then-extract THIRD          (grounds patches in quotes)
    #   4. patches FOURTH                      (new patches from this transcript)
    #   5. resolved_commitments FIFTH          (look back at prior open commits last)
    #   6. entities + relationships LAST
    "required": ["you_speaker_present", "output_language", "_reasoning", "patches", "resolved_commitments", "entities", "relationships"],
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
        "output_language": {
            "type": "string",
            "description": (
                "The language ALL output prose must be written in (patch value "
                "text, entity descriptions, relationship context). Copy the code "
                "from the 'User language:' line at the top of the input if "
                "present; otherwise use the code of the dominant language spoken "
                "by the (you) speaker (e.g. 'es', 'en', 'pt'). Every prose field "
                "you generate after this MUST be in this language, regardless of "
                "the language of any other instructions or context blocks."
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
            # Wire-side ceiling matches the backstop ceiling (see
            # extraction_patch_backstop below) — the length-scaled
            # runtime backstop is the real bound; this only stops a
            # degenerate stream from running unbounded.
            "maxItems": 64,
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
                        "required": ["text", "owner", "deadline", "deadline_date", "cues", "salience"],
                        "properties": {
                            "text": {"type": "string"},
                            "owner": {"type": ["string", "null"]},
                            "deadline": {"type": ["string", "null"]},
                            "deadline_date": {
                                "type": ["string", "null"],
                                "description": (
                                    "The deadline resolved to an absolute calendar date "
                                    "in YYYY-MM-DD form, anchored to the 'Meeting date:' "
                                    "line in the input for relative expressions like "
                                    "'tomorrow' or 'end of week'. Null when the deadline "
                                    "cannot be resolved to a specific date."
                                ),
                            },
                            "cues": {
                                "type": "array",
                                "maxItems": 5,
                                "items": {"type": "string"},
                                "description": (
                                    "0-5 short lowercase topic phrases naming what this "
                                    "patch is ABOUT — the concepts someone would mention "
                                    "in a later conversation when this memory should "
                                    "surface (e.g. 'pricing model', 'visa paperwork', "
                                    "'q3 roadmap'). NOT names of people/projects/companies "
                                    "(those belong in entities), NOT generic words like "
                                    "'meeting' or 'update', NOT sentences. Empty array "
                                    "when nothing beyond the entities applies."
                                ),
                            },
                            "salience": {
                                "type": ["string", "null"],
                                "description": (
                                    "How strongly this should be remembered. 'high' ONLY "
                                    "when the speaker signals unusual weight: emotional "
                                    "emphasis, surprise, explicit stakes ('this is "
                                    "critical', 'do not forget'), a reversal of something "
                                    "previously believed, or repetition across the "
                                    "conversation. 'low' for passing remarks unlikely to "
                                    "matter later. null for everything else — MOST "
                                    "patches are null."
                                ),
                            },
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
        "resolved_commitments": {
            "type": "array",
            "maxItems": 10,
            "description": (
                "Open commitments from the user's prior meetings that this "
                "transcript indicates are now done. Only fill if the user_content "
                "includes an `Open commitments` block AND the transcript clearly "
                "shows the action was completed. Match generously: explicit "
                "phrases like 'I sent the email', 'finished the doc', 'we shipped "
                "it' all count. If the transcript doesn't reference any of the "
                "listed commitments, emit an empty array."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["patch_id", "evidence"],
                "properties": {
                    "patch_id": {
                        "type": "string",
                        "description": (
                            "The exact patch_id from the `Open commitments` block "
                            "in user_content. Copy verbatim; do not invent or modify."
                        ),
                    },
                    "evidence": {
                        "type": "string",
                        "description": (
                            "Short quote or paraphrase from the transcript showing "
                            "the commitment was completed. Used for the audit log "
                            "and the dashboard, kept under ~300 chars."
                        ),
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
    to be persisted or returned to callers. Currently `_reasoning` and
    `output_language` (the model's language commitment — it has done its
    job once the patches are generated).

    Call after enforce_owner_gate, before handing content to the
    downstream worker pipeline.
    """
    content.pop("_reasoning", None)
    content.pop("output_language", None)
    return content


# Structured deadline dates must be bare ISO calendar dates. Anything the
# LLM emits that doesn't match (timestamps, prose, partial dates) is nulled
# rather than stored — `value.deadline` keeps the original free text, so no
# information is lost when resolution fails.
_DEADLINE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Plausibility window around the meeting date. A deadline can predate the
# meeting ("that was due yesterday") but a resolution outside this window
# is far more likely a model error (wrong year) than a real deadline.
# A deadline resolved to more than 60 days before the meeting it was
# spoken in is almost always a hallucinated year, not a real reference
# ("we were supposed to ship last month" fits comfortably inside 60).
# Was 730, which admitted wrong-year dates like 2024 on a 2026 meeting.
_DEADLINE_PAST_WINDOW = timedelta(days=60)
_DEADLINE_FUTURE_WINDOW = timedelta(days=3650)


def validate_deadline_date(raw: object, meeting_date: "date | None" = None) -> "str | None":
    """Return the normalized YYYY-MM-DD string, or None if invalid.

    Exposed separately from the sanitizer so backfill scripts can reuse
    the exact same acceptance rules on stored free-text deadlines.
    """
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not _DEADLINE_DATE_RE.match(raw):
        return None
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        return None
    if meeting_date is not None:
        if parsed < meeting_date - _DEADLINE_PAST_WINDOW:
            return None
        if parsed > meeting_date + _DEADLINE_FUTURE_WINDOW:
            return None
    return parsed.isoformat()


def classify_connection(
    label: object,
    from_type: object,
    to_type: object,
    label_specs: dict,
) -> "tuple[str, str | None]":
    """Classify one edge against the manifest connection vocabulary.

    Returns (verdict, spec_role):
      ("valid", role)     — label exists and (from_type → to_type) is allowed
      ("reversed", role)  — label exists and the exact reverse
                            (to_type → from_type) is allowed; the edge was
                            emitted backwards and can be repaired by flipping
      ("invalid", None)   — unknown label, or no orientation of this type
                            pair is allowed for the label

    `label_specs` maps label → {"role": str, "from": set, "to": set} (see
    build_label_specs). Shared by the live sanitizer and the repair
    backfill so both apply identical rules.
    """
    spec = label_specs.get(label) if isinstance(label, str) else None
    if not spec:
        return "invalid", None
    if from_type in spec["from"] and to_type in spec["to"]:
        return "valid", spec["role"]
    if to_type in spec["from"] and from_type in spec["to"]:
        return "reversed", spec["role"]
    return "invalid", None


def build_label_specs(connection_labels: "list | None") -> dict:
    """Index a manifest's connection_labels for classify_connection."""
    specs: dict = {}
    for cl in connection_labels or []:
        if not isinstance(cl, dict) or not cl.get("label"):
            continue
        specs[cl["label"]] = {
            "role": cl.get("role") or "informs",
            "from": set(cl.get("from_types") or []),
            "to": set(cl.get("to_types") or []),
        }
    return specs


def enforce_connection_vocabulary(
    content: dict, connection_labels: "list | None"
) -> dict:
    """Validate every connects_to edge against the app manifest's
    connection vocabulary (label + from/to type combos).

    The schema-driven prompt constrains edge labels to the vocabulary via
    enum, but nothing constrains WHICH type pairs a label may join, and
    the model regularly emits reversed edges (blocker blocked_by
    commitment instead of commitment blocked_by blocker) and off-spec
    combos (works_with, owns commitment→decision). Client-side validators
    drop those silently, so real semantic content is lost downstream.

    Behavior per edge:
      - valid combo      → kept; role normalized to the spec's role
      - reversed combo   → moved onto the target patch pointing back at
                           this one, when the target patch exists in this
                           output; dropped otherwise (a flip onto a
                           DB-resident patch can't be expressed here)
      - unknown label or
        invalid combo    → dropped

    No-op when connection_labels is empty/None (universal-prompt apps
    have no registered vocabulary to enforce). Audit summary recorded in
    content["_connection_vocabulary_enforced"].
    """
    if not connection_labels:
        return content
    patches = content.get("patches") or []
    if not patches:
        return content

    label_specs = build_label_specs(connection_labels)

    def _ptext(p: dict) -> str:
        v = p.get("value")
        if isinstance(v, dict):
            return (v.get("text") or "").strip()
        return ""

    by_key = {
        (_ptext(p).lower(), p.get("type")): p
        for p in patches
        if isinstance(p, dict) and _ptext(p)
    }

    kept = flipped = dropped = 0
    dropped_detail: list = []
    pending_flips: list = []  # (target_patch, new_edge) — applied after the scan

    for patch in patches:
        if not isinstance(patch, dict):
            continue
        connects = patch.get("connects_to")
        if not isinstance(connects, list) or not connects:
            continue
        from_type = patch.get("type")
        surviving: list = []
        for edge in connects:
            if not isinstance(edge, dict):
                continue
            label = edge.get("label")
            to_type = edge.get("target_type")
            verdict, spec_role = classify_connection(label, from_type, to_type, label_specs)
            if verdict == "valid":
                edge["role"] = spec_role
                surviving.append(edge)
                kept += 1
            elif verdict == "reversed":
                target = by_key.get(((edge.get("target_text") or "").strip().lower(), to_type))
                if target is not None and target is not patch:
                    pending_flips.append((
                        target,
                        {
                            "target_text": _ptext(patch),
                            "target_type": from_type,
                            "role": spec_role,
                            "label": label,
                        },
                    ))
                    flipped += 1
                else:
                    dropped += 1
                    dropped_detail.append(f"{label}:{from_type}->{to_type} (reversed, target not in output)")
            else:
                dropped += 1
                dropped_detail.append(f"{label}:{from_type}->{to_type}")
        patch["connects_to"] = surviving

    for target, new_edge in pending_flips:
        connects = target.setdefault("connects_to", [])
        duplicate = any(
            isinstance(e, dict)
            and (e.get("target_text") or "").strip().lower() == new_edge["target_text"].lower()
            and e.get("target_type") == new_edge["target_type"]
            and e.get("label") == new_edge["label"]
            for e in connects
        )
        if not duplicate:
            connects.append(new_edge)

    if flipped or dropped:
        content["_connection_vocabulary_enforced"] = {
            "kept": kept,
            "flipped": flipped,
            "dropped": dropped,
            "dropped_detail": dropped_detail[:20],
        }
    return content


def drop_placeholder_entities(content: dict) -> dict:
    """Drop diarization-label entities ("Speaker 4", "Unknown") and any
    relationships referencing them.

    The prompt bars unnamed speakers from the entities array, but the
    rule leaks (nine Speaker-N entities reached prod, two of them well
    after the rule shipped — a live lane, not just legacy). Placeholder
    entities poison the recall index: "speaker 2" as an indexed name can
    substring-match conversational text and drag in meaningless graph
    context. Uses the same predicate as the person-patch gate
    (is_placeholder_or_self_person) with no user_label, so only the
    placeholder half applies — a user's own name is a separate question
    this guard deliberately does not touch."""
    entities = content.get("entities")
    if not isinstance(entities, list):
        return content
    dropped_names = set()
    kept = []
    for ent in entities:
        name = ent.get("name") if isinstance(ent, dict) else None
        if is_placeholder_or_self_person(name):
            dropped_names.add(str(name).strip().lower())
        else:
            kept.append(ent)
    if not dropped_names:
        return content
    content["entities"] = kept

    rels = content.get("relationships")
    rels_dropped = 0
    if isinstance(rels, list):
        kept_rels = []
        for rel in rels:
            if isinstance(rel, dict) and (
                str(rel.get("from", "")).strip().lower() in dropped_names
                or str(rel.get("to", "")).strip().lower() in dropped_names
            ):
                rels_dropped += 1
            else:
                kept_rels.append(rel)
        content["relationships"] = kept_rels

    content["_placeholder_entities_enforced"] = {
        "entities_dropped": sorted(dropped_names),
        "relationships_dropped": rels_dropped,
    }
    return content


# --- Cue sanitation (associative retrieval index) -------------------------
#
# Cues are matched against raw request text on the recall hot path, so bad
# cues are worse than no cues: an ultra-generic cue ("meeting") matches
# nearly every request and floods recall with noise. Precision over recall,
# same posture as the metamemory signals.

MAX_CUES_PER_PATCH = 5
CUE_MIN_LEN = 3
CUE_MAX_LEN = 60

# Words that describe the medium, not the topic. A cue equal to one of
# these carries no associative signal.
_GENERIC_CUES = frozenset({
    "meeting", "call", "chat", "discussion", "conversation", "sync",
    "standup", "update", "updates", "status", "work", "team", "project",
    "task", "tasks", "plan", "plans", "notes", "agenda", "todo", "to do",
    "action item", "action items", "follow up", "follow-up", "followup",
    "next steps", "deadline", "general", "misc", "other", "stuff",
})


def normalize_cue_list(raw: object, cap: int = MAX_CUES_PER_PATCH) -> list:
    """Coerce a raw cues value into a clean, deduplicated, capped list of
    lowercase phrases. Never raises — junk in, empty-or-smaller list out.
    Shared by the sanitizer and the worker's defensive re-check."""
    if not isinstance(raw, list):
        return []
    out: list = []
    seen: set = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        cue = " ".join(item.lower().split())
        if not (CUE_MIN_LEN <= len(cue) <= CUE_MAX_LEN):
            continue
        if cue in _GENERIC_CUES or cue in seen:
            continue
        seen.add(cue)
        out.append(cue)
        if len(out) >= cap:
            break
    return out


def sanitize_cues(content: dict) -> dict:
    """Normalize value.cues on every patch: lowercase, dedupe, cap, drop
    generic/junk cues, and drop cues that duplicate an extracted entity
    name (the entity index already covers those — keeping them would
    double-index the same surface and bloat patch_cues)."""
    entity_names = {
        " ".join(str(e.get("name", "")).lower().split())
        for e in (content.get("entities") or [])
        if isinstance(e, dict)
    }
    entity_names.discard("")
    for patch in content.get("patches") or []:
        if not isinstance(patch, dict):
            continue
        value = patch.get("value")
        if not isinstance(value, dict):
            continue
        cues = [
            c for c in normalize_cue_list(value.get("cues"))
            if c not in entity_names
        ]
        if cues:
            value["cues"] = cues
        else:
            value.pop("cues", None)
    return content


# --- Salience sanitation (judgment-weighted encoding) ----------------------
#
# Only the two non-default levels are ever stored; "normal" is the implied
# baseline and keeping it would bloat every value JSONB for zero signal.
# The scorer boosts/penalizes on the stored levels and the decay loop
# stretches/shrinks TTL — a hallucinated level is a mis-weighted memory,
# so anything outside the vocabulary is dropped, never guessed.

VALID_SALIENCE_LEVELS = frozenset({"low", "high"})


def sanitize_salience(content: dict) -> dict:
    """Normalize value.salience on every patch: lowercase, keep only
    'low'/'high', drop 'normal'/null/junk (absent key == normal)."""
    for patch in content.get("patches") or []:
        if not isinstance(patch, dict):
            continue
        value = patch.get("value")
        if not isinstance(value, dict):
            continue
        raw = value.get("salience")
        level = raw.strip().lower() if isinstance(raw, str) else None
        if level in VALID_SALIENCE_LEVELS:
            value["salience"] = level
        else:
            value.pop("salience", None)
    return content


def sanitize_deadline_dates(content: dict, meeting_date: "date | None" = None) -> dict:
    """
    Validate LLM-emitted `value.deadline_date` fields on every patch.

    The extraction prompt asks the model to resolve free-text deadlines
    ("tomorrow", "end of week") to an absolute YYYY-MM-DD anchored to the
    meeting date. Models are unreliable about format and about years, so
    enforce both here: malformed strings and dates implausibly far from
    the meeting date are nulled. `value.deadline` (the free text) is
    never touched.
    """
    for patch in content.get("patches") or []:
        if not isinstance(patch, dict):
            continue
        value = patch.get("value")
        if not isinstance(value, dict):
            continue
        if "deadline_date" not in value:
            continue
        value["deadline_date"] = validate_deadline_date(
            value.get("deadline_date"), meeting_date
        )
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


# Patch types where the (you) speaker is implicitly the owner — setting an
# owner string on these is wrong by definition (the (you) speaker doesn't
# own anything to anyone else; the patch IS about them). Used to strip
# owner=name on trait/preference/goal/constraint patches that the LLM
# emitted in third-person form.
_SELF_TYPED_PATCH_TYPES_WITH_IMPLICIT_OWNER = frozenset(
    {"trait", "preference", "goal", "constraint"}
)


def strip_owner_on_self_typed_patches(
    content: dict, user_label: str | None = None
) -> dict:
    """Set owner=null on trait/preference/goal/constraint patches.

    The prompt instructs the model to leave owner empty on these types
    because the (you) speaker is implicitly the owner. Haiku 4.5
    occasionally ignores that and emits ``owner: "Scott"`` (or whatever
    the user_label is), which both reintroduces third-person framing
    and confuses downstream consumers that read owner as "person to
    attribute work to."

    Belt-and-suspenders: prompt says "set owner to null", this sanitizer
    enforces it post-hoc. Run after sanitize_you_marker_from_patches,
    before strip_ephemeral_fields. Mutates content in place.

    **Instrumentation, no behavior change.** Everything is still stripped.
    The rule is right in intent and too broad in effect: it also deletes a
    genuine third-party attribution ("Brightwell prefers to avoid continuous
    upgrades"), which the manifest wants expressed as a `held_by` edge
    from the preference to that person.

    Measured on prod 2026-08-04, the 12 surviving owner-carrying
    preference patches all predate this sanitizer (none since April) and
    split seven self-attributions, two placeholders, one corrupt, and two
    genuine third parties. So the wrong-kill rate looked like roughly one
    in six on legacy data.

    The problem with acting on that number is that this function destroys
    the evidence, so stored data cannot say whether the model still
    attempts third-party attribution or stopped. This records what it
    dropped, and classifies it with the same `_is_real_person_owner` gate
    the ownership backstop uses, so the conversion to `held_by` can be
    built against a measured frequency from live traffic rather than an
    inference from rows written before the rule existed.
    """
    stripped_self: list = []
    stripped_third_party: list = []

    for patch in content.get("patches") or []:
        if not isinstance(patch, dict):
            continue
        ptype = patch.get("type")
        if ptype not in _SELF_TYPED_PATCH_TYPES_WITH_IMPLICIT_OWNER:
            continue
        value = patch.get("value")
        if not (isinstance(value, dict) and value.get("owner")):
            continue
        owner = value["owner"]
        record = {
            "type": ptype,
            "owner": owner,
            "text": (value.get("text") or "")[:80],
        }
        # Real named human who is not the (you) speaker: the case the
        # manifest wants as a held_by edge and which we are deleting.
        if _is_real_person_owner(owner, user_label):
            stripped_third_party.append(record)
        else:
            stripped_self.append(record)
        value["owner"] = None

    if stripped_self or stripped_third_party:
        content["_self_typed_owner_stripped"] = {
            "self_or_placeholder": len(stripped_self),
            "third_party": len(stripped_third_party),
            "third_party_detail": stripped_third_party,
        }
    return content


# Tokens that, when they appear with spaces around them inside a
# person-patch value.text, mark the boundary between the name and a
# trailing description the LLM should not have included. The order
# doesn't matter (we use the earliest match), but the set is kept
# conservative so multi-word names like "Bramble Martinez" survive.
#
# Notably absent: " and " (occurs in "Arvind and Family"-style legit
# names — handled by split_compound_person_patches), bare hyphens
# without surrounding spaces (preserves "Jean-Luc", "O'Brien-Mayfield"),
# and " of " (preserves "Catherine of Aragon"-style; not common in our
# data but cheap to keep).
_PERSON_NAME_PROSE_SEPARATORS = (
    " — ",   # em-dash with spaces (the worst offender — prompt example used this)
    " – ",   # en-dash with spaces
    " - ",   # ASCII hyphen with spaces ("Redfern - technical lead and primary presenter")
    ", ",    # ("Speaker 5, AI tool operator and ...")
    " is ",  # ("Yardley is a developer working for ...")
    " was ",
    " has ",
    " will ",
    " who ",
)


def drop_placeholder_and_self_person_patches(
    content: dict, user_label: str | None = None
) -> dict:
    """Drop `person` patches whose value.text is a diarization placeholder
    (``"Speaker 5"``, ``"Unknown"``) or matches the (you) speaker
    themselves.

    Placeholders shouldn't become person entities — the LLM occasionally
    emits them when a transcript only has diarized labels with no real
    names. Self-reference person patches (e.g. ``"Scott"`` in Scott's
    quilt) are also wrong: the (you) speaker's attribution is implicit
    via subject_key, so a third-party-style person patch about themselves
    is a duplicate that confuses recall and clutters the dashboard.

    ``enforce_person_ownership`` already guards against creating these
    via its safety net (via ``_is_real_person_owner``). This sanitizer
    catches the case where the LLM emitted the placeholder/self-reference
    person patch *directly* in its output, bypassing the enforcer.

    Behavior:
      - Detects placeholders by lowercase prefix
        (``_OWNER_PLACEHOLDER_PREFIXES``).
      - Detects self-reference by case-insensitive equality of trimmed
        ``value.text`` to ``user_label`` (when provided).
      - Removes matching patches from ``content["patches"]`` in place.
      - Removes any other patch's ``connects_to`` entry whose
        ``target_text`` referenced a dropped name (avoids dangling
        edges).

    Pass ``user_label=None`` to skip the self-reference check (e.g. when
    the (you) speaker isn't known at extraction time).
    """
    patches = content.get("patches") or []
    if not patches:
        return content

    drop_names: set[str] = set()
    kept: list[dict] = []

    # Names this SAME extraction called something other than a person.
    # CIGNA reached the People list as a colleague on 2026-08-17: the
    # model emitted it as an `org` entity AND as a `person` patch in one
    # response, the person patch minted a person entity, and an insurance
    # company appeared under NEW FACES with "Joined 1 of your rooms this
    # week". The contradiction is visible inside the payload, so it does
    # not need a database lookup to catch: a name cannot be a company and
    # a colleague in the same meeting.
    non_person_names: set[str] = set()
    for ent in content.get("entities") or []:
        if not isinstance(ent, dict):
            continue
        ent_type = ent.get("type")
        ent_name = ent.get("name")
        if (isinstance(ent_type, str) and ent_type != "person"
                and isinstance(ent_name, str) and ent_name.strip()):
            non_person_names.add(ent_name.strip().lower())

    for patch in patches:
        if patch.get("type") != "person":
            kept.append(patch)
            continue
        value = patch.get("value")
        text = value.get("text") if isinstance(value, dict) else None
        if not isinstance(text, str) or not text.strip():
            kept.append(patch)
            continue

        if is_placeholder_or_self_person(text, user_label):
            drop_names.add(text.strip().lower())
            continue
        if text.strip().lower() in non_person_names:
            drop_names.add(text.strip().lower())
            continue
        kept.append(patch)

    if not drop_names:
        return content

    content["patches"] = kept

    # Strip connects_to entries pointing at the dropped names (would
    # otherwise produce dangling edges during connection resolution).
    for patch in kept:
        connects = patch.get("connects_to")
        if not isinstance(connects, list):
            continue
        patch["connects_to"] = [
            c
            for c in connects
            if not (
                isinstance(c, dict)
                and isinstance(c.get("target_text"), str)
                and c["target_text"].strip().lower() in drop_names
            )
        ]
    return content


def strip_prose_from_person_names(content: dict) -> dict:
    """Truncate trailing prose from person-patch value.text.

    The LLM occasionally writes a sentence into a person patch's name
    field (``"Ashby - customer success point of contact for ..."``)
    when the prompt asked for a name. The dashboard then renders that
    sentence as the person's display name and the entity index can't
    match it against future mentions of just "Ashby".

    This sanitizer finds the FIRST occurrence of any of the
    _PERSON_NAME_PROSE_SEPARATORS in value.text and truncates to the
    prefix. Conservative: only acts when a separator is present, never
    blindly truncates by length. If the resulting prefix is empty or
    shorter than 2 chars, the original text is kept (don't corrupt
    data we can't confidently fix).

    Mutates content in place. Same belt-and-suspenders pattern as
    strip_owner_on_self_typed_patches.
    """
    for patch in content.get("patches") or []:
        if patch.get("type") != "person":
            continue
        value = patch.get("value")
        if not isinstance(value, dict):
            continue
        text = value.get("text")
        if not isinstance(text, str):
            continue

        cut: int | None = None
        for sep in _PERSON_NAME_PROSE_SEPARATORS:
            idx = text.find(sep)
            if idx >= 0 and (cut is None or idx < cut):
                cut = idx
        if cut is None:
            continue

        cleaned = text[:cut].strip()
        if len(cleaned) < 2:
            continue
        value["text"] = cleaned
    return content


def sanitize_behavior_observations(content: dict) -> dict:
    """Drop behavioral observations that state character instead of conduct.

    Guardrail 12b says a claim cites observable behavior and never
    character: "reopens vague commitments" is something a reader can check
    against a transcript, "insecure" is a verdict about a human being. The
    guardrail was written for the claim the profile pass writes, but the
    pass can only be as good as what it reads, and behavioral observations
    are captured at extraction. So the rule is enforced here too, on the
    corpus, and not only on the sentence written from it.

    Dropping rather than rewriting is deliberate. There is no honest
    repair for "Yardley is defensive about review feedback": the observable
    thing that provoked it was not recorded, so anything we kept would be
    the verdict with the evidence still missing. A dropped observation
    costs one row; a stored one poisons every lens that later reads this
    person's corpus, and does it invisibly.

    Two mechanics that matter:

    - Only types in BEHAVIOR_OBSERVATION_TYPES are inspected. Every other
      type passes through untouched, so a manifest that declares no such
      type gets byte-identical output.
    - Any other patch's `connects_to` entry pointing at a dropped
      observation is stripped, exactly as the placeholder-person sanitizer
      does. Without that, the Pass-2 resolver in `store_connected_patches`
      finds an unresolved target and SYNTHESIZES a stub patch carrying the
      same text, which would put the dropped verdict back in the quilt
      with a lower confidence and no origin.

    English only, and the limit is real rather than theoretical:
    extraction writes in the language of the meeting, so a character
    verdict in Spanish or Japanese passes this function untouched and is
    governed only by the manifest guidance the model was shown. See
    `follow_through.CHARACTER_TRAIT_WORDS`.

    Mutates content in place; returns it. Records what it dropped in
    ``content["_behavior_observations_sanitized"]``.
    """
    patches = content.get("patches")
    if not isinstance(patches, list) or not patches:
        return content

    dropped: list[dict] = []
    dropped_targets: set[tuple] = set()
    kept: list[dict] = []

    for patch in patches:
        if not isinstance(patch, dict) or patch.get("type") not in BEHAVIOR_OBSERVATION_TYPES:
            kept.append(patch)
            continue
        value = patch.get("value")
        text = value.get("text") if isinstance(value, dict) else None
        if not isinstance(text, str) or not text.strip():
            kept.append(patch)
            continue
        word = character_word_in(text, CHARACTER_WORDS + CHARACTER_TRAIT_WORDS)
        if word is None:
            kept.append(patch)
            continue
        dropped.append({
            "type": patch.get("type"),
            "text": text[:120],
            "word": word,
        })
        dropped_targets.add((patch.get("type"), text.strip().lower()))

    if not dropped:
        return content

    content["patches"] = kept
    for patch in kept:
        connects = patch.get("connects_to")
        if not isinstance(connects, list):
            continue
        patch["connects_to"] = [
            c for c in connects
            if not (
                isinstance(c, dict)
                and isinstance(c.get("target_text"), str)
                and (c.get("target_type"), c["target_text"].strip().lower())
                in dropped_targets
            )
        ]

    content["_behavior_observations_sanitized"] = {
        "dropped": dropped,
        "count": len(dropped),
    }
    return content


# ------------------------------------------------------------------
# Manifest-declared storage behavior
# ------------------------------------------------------------------
# Two per-type opt-ins the storage sink reads. Both are absent from every
# manifest registered before this change, and absent means today's
# behavior, so nothing that ships now moves.


def _flagged_patch_types(manifest: object, key: str, want: bool) -> frozenset:
    """Every declared domain_type whose `key` flag is exactly `want`."""
    if not isinstance(manifest, dict):
        return frozenset()
    out = set()
    for pt in manifest.get("patch_types") or []:
        if not isinstance(pt, dict):
            continue
        name = pt.get("domain_type")
        if isinstance(name, str) and name and pt.get(key) is want:
            out.add(name)
    return frozenset(out)


def no_collapse_patch_types(manifest: object) -> frozenset:
    """Types that declared `collapse_duplicates: false`.

    Storage dedup is type-blind: the trigram fast path merges any two
    same-type patches whose text is similar enough, which is right for a
    fact ("ship the API by Friday" said twice is one commitment) and
    destructive for an observation. Two observations of the same behavior
    in two meetings are a TRAJECTORY, which is the same argument doc 12
    section 3.1 makes about ratings. Worse, a collapse keeps only the
    surviving patch's origin_id, so it silently destroys a receipt, and
    the profile pass gates on distinct meetings.

    Deliberately NOT the existing `longitudinal` flag. That one carries
    series identity semantics (a descriptor field, a patch_observations
    history, one row per series) that answer a different question, and it
    is wired only into structured ingest. This flag says one thing:
    never merge two of these, ever.
    """
    return _flagged_patch_types(manifest, "collapse_duplicates", False)


def origin_scoped_patch_types(manifest: object) -> frozenset:
    """Types that declared `origin_scoped: true`.

    The storage sink stamps `origin_id` only on project-scoped types, so a
    type that is meeting-bound but NOT project-bound lands with a null
    origin: no receipt, invisible to the meeting view, and structurally
    unclusterable by the profile pass, whose cluster query requires
    `origin_id IS NOT NULL` and counts distinct origins as meetings.

    This flag separates the two ideas that gate used to conflate. Project
    scoping says WHICH project a patch belongs to; origin scoping says
    the patch records something that happened at a particular moment and
    must remember which one. A patch type can now say the second without
    claiming the first.
    """
    return _flagged_patch_types(manifest, "origin_scoped", True)


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


# Stopwords used by _best_matching_deliverable. Kept small and conservative —
# we want short content words like "API", "doc", "patch" to count, but we
# need to filter out the high-frequency glue words that would inflate every
# pairing's overlap score. NOT a general English stopword list; tuned for
# meeting-extraction text (verbs of action, deictics, conjunctions).
_DELIV_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "and", "or", "but", "if", "then", "so",
    "of", "in", "on", "at", "by", "for", "to", "from", "with", "as", "into",
    "this", "that", "these", "those", "it", "its",
    "i", "you", "he", "she", "we", "they",
    "do", "does", "did", "have", "has", "had",
    "will", "would", "should", "could", "can", "may", "might",
    "not", "no", "yes", "all", "any", "some",
    "after", "before", "during", "while", "until",
})


def _content_words(text: str) -> set[str]:
    """Lowercase, split on word boundaries, drop stopwords + tokens <3 chars."""
    if not text:
        return set()
    tokens = []
    cur: list[str] = []
    for ch in text.lower():
        if ch.isalnum() or ch == "'":
            cur.append(ch)
        else:
            if cur:
                tokens.append("".join(cur))
                cur = []
    if cur:
        tokens.append("".join(cur))
    return {t for t in tokens if len(t) > 2 and t not in _DELIV_STOPWORDS}


def _best_matching_deliverable(
    orphan_text: str, deliverable_texts: list[str]
) -> str | None:
    """Return the deliverable text whose content words overlap most with
    the orphan, or None if there's no clear winner.

    "Clear winner" requires:
      - best overlap >= 2 content words (avoids matches off a single
        coincidental word like "API")
      - best overlap strictly greater than runner-up (no ties)

    Conservative on purpose. The pre-fix behavior was to fall back to
    the meeting project for the multi-deliverable case — a safe default
    that loses granularity. This function only overrides that default
    when the text evidence is unambiguous; otherwise it returns None
    and the caller falls back to project, same as before.
    """
    orphan_words = _content_words(orphan_text)
    if not orphan_words or len(deliverable_texts) < 2:
        return None
    scored = []
    for dtext in deliverable_texts:
        overlap = len(orphan_words & _content_words(dtext))
        scored.append((overlap, dtext))
    scored.sort(key=lambda t: (-t[0], t[1]))  # desc by overlap, tiebreak alphabetical
    best_score, best_text = scored[0]
    runner_up_score = scored[1][0] if len(scored) > 1 else 0
    if best_score < 2:
        return None
    if best_score == runner_up_score:
        return None
    return best_text


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

    Auto-parent target selection (v1.2):
      - If exactly ONE `deliverable` patch is present in the same output
        AND the orphan patch type is in DELIVERABLE_CHILD_TYPES, parent
        to the deliverable (narrowest valid parent).
      - If TWO OR MORE deliverables are present and the orphan type is
        in DELIVERABLE_CHILD_TYPES, score the orphan text against each
        deliverable text by content-word overlap. If there's a clear
        winner (best overlap >= 2 words AND strictly greater than
        runner-up), parent to that deliverable. Otherwise fall back to
        meeting_project (the safe default).
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
    # Single deliverable → trivial: it's the only viable child target.
    # Multiple deliverables → no single "preferred" — selection happens
    # per-orphan via _best_matching_deliverable below.
    preferred_deliverable = (
        deliverable_texts[0] if len(deliverable_texts) == 1 else None
    )

    valid_parent_texts = project_texts | set(deliverable_texts)

    kept: list = []
    dropped: list = []
    auto_parented: list = []

    def _inject_parent(patch: dict) -> None:
        ptype = patch.get("type")
        target_text = None
        target_type = None
        if ptype in DELIVERABLE_CHILD_TYPES:
            if preferred_deliverable is not None:
                target_text = preferred_deliverable
                target_type = "deliverable"
            elif len(deliverable_texts) >= 2:
                # Multi-deliverable case: pick the deliverable whose text
                # has the strongest content-word overlap with the orphan
                # text. Falls back to project when there's no clear
                # winner — this preserves the pre-fix safe behavior for
                # truly ambiguous cases.
                match = _best_matching_deliverable(
                    (patch.get("value") or {}).get("text", ""),
                    deliverable_texts,
                )
                if match is not None:
                    target_text = match
                    target_type = "deliverable"
        if target_text is None:
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
# `owns` connection vocabulary: from={person}
# to={commitment,blocker,decision,goal,behavior}.
#
# `behavior` is here because the ownership edge is the ONLY way anything
# reaches a person: the profile pass clusters on it, and a type absent
# from this set can be declared in a manifest, extracted, stored, and
# still never be seen by a lens. A behavioral observation whose whole
# purpose is to describe one named person is the strongest member of the
# set, not an exception to it.
#
# Still a hardcoded SS name list, and still the read side's standing debt
# (project_facet_runtime_debt). The general form is already sitting in
# every manifest: this set IS `connection_labels[label=owns].to_types`,
# which for SS v11 is exactly these five names. Deriving it needs the
# manifest threaded into the enforcer and its mirror, which is a wider
# change than this one earns.
PERSON_OWNED_ACTION_TYPES = frozenset(
    {"commitment", "blocker", "decision", "goal", "behavior"}
)

# Types whose text is a claim about how a named human conducted
# themselves, and which are therefore held to guardrail 12b at capture
# time: cite observable behavior, never character. Same hardcoded-name
# caveat as the set above.
BEHAVIOR_OBSERVATION_TYPES = frozenset({"behavior"})

# Owner-text values that MUST NOT trigger a synthetic person patch:
# - the (you) speaker (their attribution is implicit via patch ownership)
# - diarization placeholders that aren't real human names
# - empty / unknown markers
_OWNER_PLACEHOLDER_PREFIXES = ("speaker ", "speaker_", "unknown", "unidentified")
_OWNER_YOU_TOKENS = frozenset({"(you)", "you", "self", "me", "i", "myself"})


def is_placeholder_or_self_person(text: object, user_label: "str | None" = None) -> bool:
    """True when a person name is a diarization placeholder ("Speaker 3",
    "Unknown") or the (you) speaker themselves.

    Single source of truth for the self/placeholder person gate — used by
    drop_placeholder_and_self_person_patches (LLM-emitted person patches)
    and by store_connected_patches Pass-2 stub synthesis (connects_to
    targets), which previously bypassed the gate and re-created the very
    self-person patch the sanitizer had just dropped.
    """
    if not isinstance(text, str) or not text.strip():
        return False
    low = text.strip().lower()
    if any(low.startswith(p) for p in _OWNER_PLACEHOLDER_PREFIXES):
        return True
    if isinstance(user_label, str) and user_label.strip() and low == user_label.strip().lower():
        return True
    return False


# Transcript speaker labels look like "[Fenwick] ..." at line start. Bounded
# length so a stray bracket in prose cannot swallow a paragraph.
SPEAKER_LABEL = re.compile(r"^\s*\[([^\]]{1,60})\]", re.MULTILINE)


def speaker_turn_counts(text: object, user_label: "str | None" = None) -> dict:
    """Lowercased speaker label -> number of turns in a transcript.

    Single source of truth for "who actually spoke, and how much". The
    transcript is in hand exactly once (ingest); derive-then-discard,
    because transcripts are not retained and this signal can never be
    backfilled (the design-12a audit's hardest constraint). Turn counts
    feed per-appearance metrics: the capacity-gate turn-count refinement
    (a 1-turn label against a 41-turn label in one meeting is a
    diarization artifact, not a second person) and, eventually, the
    briefing's engagement lens.

    Measured on the ABM meeting of 2026-07-28: speaker labels are clean and
    consistent (Ellery appears as a label 23 times, spelled correctly every
    time) while the same names inside spoken text are not ("Palavi" for
    Garrick, "Fenwyck" for Fenwick, "JN" and "JNZ" for Ellery). Labels are a
    trustworthy identity signal; mention text is not. That asymmetry is why
    the speaker capacity is worth recording separately from the mention one.

    The `(you)` marker is stripped so a label matches the entity name it was
    extracted from. Diarization placeholders ("Speaker 3", "Unknown") are
    dropped: they are not people and must never gate an identity decision.
    """
    if not isinstance(text, str) or not text:
        return {}
    counts: dict = {}
    for raw in SPEAKER_LABEL.findall(text):
        name = raw.replace("(you)", "").strip()
        if not name or is_placeholder_or_self_person(name, user_label):
            continue
        key = name.lower()
        counts[key] = counts.get(key, 0) + 1
    return counts


# One turn: a speaker label and everything said until the next label.
_TURN = re.compile(r"^\s*\[([^\]]{1,60})\]([^\[]*)", re.MULTILINE)
# Sentences, keeping the terminator so a question can be recognized by it.
_SENTENCE = re.compile(r"[^.!?\n]+[.!?]+|[^.!?\n]+")
# Openers that sit in front of a vocative and are not part of the name.
_VOCATIVE_FILLER = frozenset({
    "hey", "hi", "hello", "ok", "okay", "so", "and", "but", "well",
    "um", "uh", "yeah", "yep", "right", "look", "listen", "sorry",
})
_NAME_EDGE = re.compile(r"^[\s\"'`().,!?-]+|[\s\"'`().,!?-]+$")


def _clean_name(fragment: str) -> str:
    return _NAME_EDGE.sub("", fragment or "").strip().lower()


def _strip_vocative_filler(fragment: str) -> str:
    """Drop leading interjections so "Hey Marcus" reads as "Marcus"."""
    tokens = _clean_name(fragment).split()
    while tokens and tokens[0] in _VOCATIVE_FILLER:
        tokens = tokens[1:]
    return " ".join(tokens)


def _addressee_vocabulary(labels: "set") -> dict:
    """Every spoken form that unambiguously names one speaker label.

    The full label, plus its first token when no other speaker in the
    room shares it. Two Marcuses in one meeting means "Marcus" names
    nobody in particular, and an ambiguous vocative must fall through to
    the inferred column rather than pick one at random.
    """
    vocab: dict = {}
    first_tokens: dict = {}
    for label in labels:
        vocab[label] = label
        head = label.split()[0] if label.split() else ""
        if head and head != label:
            first_tokens.setdefault(head, set()).add(label)
    for head, owners in first_tokens.items():
        if len(owners) == 1 and head not in vocab:
            vocab[head] = next(iter(owners))
    return vocab


def _explicit_addressee(question: str, vocab: dict, speaker: str) -> "str | None":
    """The speaker label a question NAMES as its addressee, or None.

    A vocative is comma delimited and sits at an edge of the sentence:
    "Marcus, can you get me that?" and "Can you get me that, Marcus?".
    A name in the middle of a clause is a name being TALKED ABOUT, not
    an addressee, and the difference is the whole reliability of this
    column: "Did Marcus ever send that?" asked of somebody else names
    Marcus and is addressed to the person across the table. That case
    returns None here and falls through to the inferred column, where a
    client can choose not to trust it. Reading it as explicit would put
    the user's follow up pressure on the wrong person's row with the
    high confidence label attached, which is the one error this design
    cannot absorb.

    A question that is nothing but a name ("Marcus?") is explicit too.
    """
    body = _NAME_EDGE.sub("", question or "")
    if not body:
        return None
    parts = [p for p in body.split(",")]
    candidates = []
    if len(parts) >= 2:
        candidates.append(_strip_vocative_filler(parts[0]))
        candidates.append(_clean_name(parts[-1]))
    else:
        candidates.append(_clean_name(body))
    for c in candidates:
        target = vocab.get(c)
        if target and target != speaker:
            return target
    return None


def question_attribution(text: object, user_label: "str | None" = None) -> dict:
    """Who asked the questions in a transcript, and who they were asked OF.

    The sibling of `speaker_turn_counts`, and it exists for the same
    reason and under the same constraint: the transcript is in hand
    exactly once, at ingest, and this signal can NEVER be backfilled.
    Every meeting that lands before this ships is permanently
    unmeasurable, which is why it is worth capturing before there is a
    surface that reads it.

    What it is for: follow up pressure. CQ can already say what each
    person owes and how their items closed. It cannot say who the user
    actually presses, and the interesting question about a set of
    meetings is whether those two line up. This module measures; it
    draws no conclusion, computes no ratio, and names no asymmetry.

    Attribution runs in three grades and they are NEVER summed:

    - EXPLICIT: the question names its addressee as a vocative. High
      confidence, see `_explicit_addressee`.
    - INFERRED: the question ends the turn and somebody else speaks
      next, so the addressee is taken to be them. A heuristic, and
      wrong sometimes: a person who ignores a question asked to the room
      and changes the subject collects it. Kept in its own column so a
      client can trust the explicit one alone, which it could never do
      again if the two were blended into a number.
    - UNATTRIBUTED: a question to the room with nothing to attribute it
      to. Counted, never dropped, because it is the denominator that
      says how much of the meeting this measurement missed.

    Rhetorical questions are the reason inference only fires on a turn's
    TRAILING questions (the ones after its last statement sentence): "Why
    did that slip? Because legal." answers itself, and the next speaker
    did not receive it. "Why did that slip? Because legal. Can you fix
    it?" attributes only the last one.

    Same hygiene as `speaker_turn_counts`: `(you)` stripped, diarization
    placeholders dropped, label keys lowercased. The user is not a row in
    `by_label` (they have no appearance row of their own); their side is
    the `user` block, and it is None when no label could be identified as
    theirs, which makes every `from_user_*` count None as well. That is a
    cannot-tell, not a zero: the counts would otherwise read as "the user
    asked this person nothing".
    """
    empty = {
        "by_label": {},
        "user": None,
        "unattributed": 0,
        "questions_total": 0,
    }
    if not isinstance(text, str) or not text:
        return empty

    # Pass 1: the turns, with the self label resolved from either signal
    # (the inline marker or the passed display name).
    turns: list = []
    self_key: "str | None" = None
    for m in _TURN.finditer(text):
        raw, body = m.group(1), m.group(2)
        marked = "(you)" in raw.lower()
        name = re.sub(r"\(you\)", "", raw, flags=re.IGNORECASE).strip()
        if not name or is_placeholder_or_self_person(name):
            turns.append((None, body))
            continue
        key = name.lower()
        if marked or is_placeholder_or_self_person(name, user_label):
            self_key = self_key or key
        turns.append((key, body))
    if not turns:
        return empty

    labels = {k for k, _ in turns if k}
    vocab = _addressee_vocabulary(labels)
    others = sorted(labels - ({self_key} if self_key else set()))

    def _blank() -> dict:
        return {
            "asked": 0,
            "received_explicit": 0,
            "received_inferred": 0,
            # Null, not zero, when CQ cannot tell which speaker is the
            # user. See the docstring.
            "from_user_explicit": 0 if self_key else None,
            "from_user_inferred": 0 if self_key else None,
        }

    by_label = {k: _blank() for k in others}
    user_block = {"asked": 0, "received_explicit": 0, "received_inferred": 0} if self_key else None
    unattributed = 0
    total = 0

    for idx, (speaker, body) in enumerate(turns):
        if not speaker or not body.strip():
            continue
        sentences = [s.strip() for s in _SENTENCE.findall(body) if s.strip()]
        if not sentences:
            continue
        # Trailing questions: everything after the turn's last statement.
        last_statement = max(
            (i for i, s in enumerate(sentences) if not s.endswith("?")),
            default=-1,
        )
        # Only the IMMEDIATE next turn can be an answer. Skipping over a
        # placeholder turn to find a named one would attribute a question
        # across somebody else's reply, so a placeholder next turn (None
        # here) leaves the question unattributed.
        next_speaker = turns[idx + 1][0] if idx + 1 < len(turns) else None

        for i, sentence in enumerate(sentences):
            if not sentence.endswith("?"):
                continue
            total += 1
            if speaker == self_key:
                user_block["asked"] += 1
            else:
                by_label[speaker]["asked"] += 1

            target = _explicit_addressee(sentence, vocab, speaker)
            grade = "explicit"
            if target is None and i > last_statement and next_speaker and next_speaker != speaker:
                target, grade = next_speaker, "inferred"
            if target is None:
                unattributed += 1
                continue
            if target == self_key:
                user_block[f"received_{grade}"] += 1
                continue
            slot = by_label.get(target)
            if slot is None:
                unattributed += 1
                continue
            slot[f"received_{grade}"] += 1
            if self_key and speaker == self_key:
                slot[f"from_user_{grade}"] += 1

    return {
        "by_label": by_label,
        # The denominator for every from_user count: how many questions
        # the user asked in this meeting at all, attributed or not.
        "user": user_block,
        "unattributed": unattributed,
        "questions_total": total,
    }


def self_speaker_label(text: object) -> "str | None":
    """The (you)-marked speaker's name from a transcript, lowercased, or
    None when no marker is present.

    Single source of truth for "which speaker IS the submitting user"
    when the marker arrives inline (SS injects "[Scott (you)]" client
    side after voice match) and metadata carries no owner_speaker_label.
    Feeds the self-entity stamp in store_entities: the ego link the 13b
    orbit graph excludes.

    Placeholders never qualify: "[Speaker 2 (you)]" is a diarization
    artifact wearing the marker, and stamping it would pin the ego to a
    row the placeholder gate exists to keep out of identity decisions.
    """
    if not isinstance(text, str) or not text:
        return None
    for raw in SPEAKER_LABEL.findall(text):
        if "(you)" not in raw.lower():
            continue
        name = re.sub(r"\(you\)", "", raw, flags=re.IGNORECASE).strip()
        if not name or is_placeholder_or_self_person(name):
            continue
        return name.lower()
    return None


def speaker_labels_in(text: object, user_label: "str | None" = None) -> set:
    """Lowercased speaker labels appearing in a transcript.

    Derived from speaker_turn_counts so the two can NEVER disagree about
    who spoke (one parser, one placeholder gate — the shared-predicate
    rule). Kept for the appearance writer and backfill call sites that
    only need membership.
    """
    return set(speaker_turn_counts(text, user_label))


def is_user_reference(name: object, user_label: "str | None" = None) -> bool:
    """True when a name refers to the submitting user themselves.

    Deliberately broader than `is_placeholder_or_self_person`, which
    requires the display name to match exactly. The extractor writes the
    user's FIRST name far more often than their full one, and for the
    self case an exact-match-only test is a hole rather than a
    conservative choice: it lets "Scott" through as a counterparty on an
    item owned by "Scott Guida", which renders as the user owing
    themselves.

    Caught on the first production dry run of the owed_to backfill:
    "Scott to obtain feature request number", owner "Scott Guida",
    proposed owed_to "Scott". There is a real person patch named "Scott"
    for this user, so the edge would have been written and the People
    card would have shown the user an obligation to themselves.

    Kept separate from `is_placeholder_or_self_person` rather than
    widening it, because that predicate already gates which person
    patches get dropped, and broadening it there would change what
    extraction stores for every app.
    """
    if not isinstance(name, str):
        return False
    s = name.strip()
    if not s:
        return False
    low = s.lower()
    if low in _OWNER_YOU_TOKENS:
        return True
    if not user_label or not user_label.strip():
        return False
    label = user_label.strip().lower()
    if low == label:
        return True
    parts = label.split()
    return len(parts) > 1 and low == parts[0]


def _split_compound_owner(owner_text: str | None) -> list[str]:
    """Split a slash-joined owner string into individual name parts.

    The LLM occasionally emits joint owners like ``"Marlowe/Quill"`` or
    ``"Zephyra/Yardley"`` when a transcript line says "Marlowe and Quill will
    handle it." Without splitting, the enforcer creates one synthetic
    person patch with the literal compound text and a single owns edge —
    losing the per-person attribution.

    Conservative splitter: ``/`` only. We don't split on ``,``, ``&``,
    or ``" and "`` — those legitimately appear inside single names
    ("Mayfield, Corwin", "AT&T", "Arvind and family") and over-splitting would
    fragment real names. Slash is unambiguous in human-name contexts.

    Returns the list of trimmed parts. Single-part owners are returned
    as a one-element list. Empty / None / whitespace-only input returns
    an empty list. Empty parts after split are dropped.
    """
    if not owner_text:
        return []
    s = owner_text.strip()
    if not s:
        return []
    if "/" not in s:
        return [s]
    return [p.strip() for p in s.split("/") if p.strip()]


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
    Action items routinely come back with `value.owner: "Thorne"` and no
    Thorne person patch and no `owns` connection. This is the structural
    safety net — same shape as enforce_connection_requirements for parents.

    For each commitment/blocker/decision/goal in content["patches"]:
      1. Read value.owner.
      2. Split slash-joined compound owners ("Marlowe/Quill") into
         individual names — each gets its own person patch + owns edge.
      3. For each split name: skip if empty, the (you) speaker, or a
         diarization placeholder ("Speaker N", "Unknown").
      4. Find an existing person patch in patches[] whose value.text
         matches the name (case-insensitive).
      5. If absent, inject a synthetic person patch.
      6. Ensure a `person → action_item` `owns` connection exists. If
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
        target_text = (p.get("value") or {}).get("text") or ""
        if not target_text:
            continue
        # Split compound owners ("Marlowe/Quill") into individual names
        # so each gets a real person patch and an owns edge. Single-name
        # owners pass through as a one-element list.
        for name in _split_compound_owner(owner):
            if not _is_real_person_owner(name, user_label):
                continue
            person = _ensure_person(name)
            _ensure_owns_edge(person, target_text, p.get("type"))

    if persons_injected or connections_injected:
        content["_person_ownership_enforced"] = {
            "persons_injected": persons_injected,
            "connections_injected": connections_injected,
        }
    return content


def enforce_owner_edge_agreement(
    content: dict, user_label: str | None = None
) -> dict:
    """
    Drop `owns` edges into an action item from anyone who is not that
    item's stated owner.

    This is the other half of enforce_person_ownership. That one guarantees
    the *presence* of an owns edge for every named owner; this guarantees
    their *absence* for everyone else.

    The failure it fixes: the extractor attaches a second person to a
    commitment when the transcript involves them in some other capacity,
    because `owns` is the only person-to-item label most manifests define.
    Any involvement the vocabulary cannot name gets recorded as ownership,
    and nothing downstream can tell a real owner from a bystander.

    Observed live (ABM, meeting of 2026-07-28): "Configure IP whitelisting
    for new non-prod environment; coordinate with Denby on turnaround
    time" carried owns edges from BOTH Denby, the real owner, and
    Ellery, whose actual role was supplying the IP address the work waits
    on. The app rendered both as owners because that is what we stored.

    Conservative by construction:
      - An action item with no usable stated owner is left completely
        alone. With no stated owner there is nothing to filter against,
        and dropping edges there would discard the only ownership signal
        the patch has.
      - Compound owners ("Marlowe/Quill") keep every edge, using the same
        split that enforce_person_ownership used to create them.
      - Only `owns` edges are touched. Every other label passes through.
    """
    patches = content.get("patches")
    if not isinstance(patches, list):
        return content

    dropped: list[dict] = []

    # Stated-owner name set per action item, keyed by (type, text). Only
    # items that survive the real-owner filter get an entry; everything
    # else is deliberately absent so the edge walk below skips it.
    allowed_by_target: dict[tuple, set] = {}
    for p in patches:
        if not isinstance(p, dict) or p.get("type") not in PERSON_OWNED_ACTION_TYPES:
            continue
        value = p.get("value") or {}
        text = (value.get("text") or "").strip()
        if not text:
            continue
        allowed = {
            name.strip().lower()
            for name in _split_compound_owner(value.get("owner"))
            if _is_real_person_owner(name, user_label)
        }
        if allowed:
            allowed_by_target[(p.get("type"), text.lower())] = allowed

    if not allowed_by_target:
        return content

    for p in patches:
        if not isinstance(p, dict) or p.get("type") != "person":
            continue
        person_name = ((p.get("value") or {}).get("text") or "").strip().lower()
        edges = p.get("connects_to")
        if not isinstance(edges, list):
            continue
        kept = []
        for c in edges:
            if not isinstance(c, dict) or c.get("label") != "owns":
                kept.append(c)
                continue
            key = (
                c.get("target_type"),
                (c.get("target_text") or "").strip().lower(),
            )
            allowed = allowed_by_target.get(key)
            if allowed is None or person_name in allowed:
                kept.append(c)
                continue
            dropped.append(
                {
                    "person": (p.get("value") or {}).get("text", ""),
                    "target_type": c.get("target_type"),
                    "target_text": c.get("target_text"),
                }
            )
        if len(kept) != len(edges):
            p["connects_to"] = kept

    if dropped:
        content["_owner_edge_agreement_enforced"] = {"dropped": dropped}
    return content


def enforce_owed_to_counterparty(
    content: dict, user_label: str | None = None
) -> dict:
    """
    Drop `owed_to` edges that name a counterparty who cannot be one.

    `owed_to` runs FROM an action item TO the person waiting on it, and it
    is the only thing in the vocabulary that can say *you owe it to her*.
    That makes it the field behind the left column of the People ledger,
    and two of its failure modes produce a confidently wrong sentence
    rather than a missing one:

      - **Owed to its own owner.** "Denby will send Denby the IP
        address" is not a counterparty, it is the model restating the
        owner in a second slot. Kept, it renders as a person owing
        themselves.
      - **Owed to the (you) speaker.** Real relationship, wrong
        representation. *Lockridge owes you* is already carried by Lockridge's
        `owns` edge, and the (you) speaker has no person patch by design
        (see drop_placeholder_and_self_person_patches). An edge pointing
        at them dangles, and Pass-2 stub synthesis would answer the dangle
        by re-creating the self person patch that the self gate exists to
        prevent.

    Diarization placeholders ("Speaker 4") go the same way as the self
    case: a counterparty CQ cannot name is not a counterparty it can bill
    a user for.

    Conservative in the same shape as enforce_owner_edge_agreement: an
    item with no usable stated owner keeps every counterparty edge it has,
    because with no owner there is nothing to contradict. Compound owners
    ("Marlowe/Quill") filter against every part. Only `owed_to` edges are
    touched.

    Known limit, stated rather than papered over: owner and counterparty
    are compared as STRINGS, so two surface forms of the same third party
    ("Lockridge" as owner, "Lockridge Chen" as counterparty) are not recognised as
    one human and the edge survives. Entity aliasing knows that; this
    sanitizer runs before anything is stored and has only the text in
    front of it. The self case is exempt because `is_user_reference`
    covers the first-name form explicitly, and the self case is the one
    that produces a user-visible lie.

    Audit detail in content["_owed_to_enforced"].
    """
    patches = content.get("patches")
    if not isinstance(patches, list):
        return content

    dropped: list[dict] = []

    for p in patches:
        if not isinstance(p, dict):
            continue
        edges = p.get("connects_to")
        if not isinstance(edges, list) or not edges:
            continue

        value = p.get("value") or {}
        owners = {
            name.strip().lower()
            for name in _split_compound_owner(value.get("owner"))
            if name and name.strip()
        }

        kept: list = []
        for c in edges:
            if not isinstance(c, dict) or c.get("label") != "owed_to":
                kept.append(c)
                continue
            target = (c.get("target_text") or "").strip()
            if not target:
                dropped.append({"item": value.get("text", ""), "target": target, "why": "empty"})
                continue
            if is_placeholder_or_self_person(target, user_label) or is_user_reference(
                target, user_label
            ):
                dropped.append({"item": value.get("text", ""), "target": target, "why": "self_or_placeholder"})
                continue
            if target.lower() in owners:
                dropped.append({"item": value.get("text", ""), "target": target, "why": "owed_to_own_owner"})
                continue
            kept.append(c)

        if len(kept) != len(edges):
            p["connects_to"] = kept

    if dropped:
        content["_owed_to_enforced"] = {"dropped": dropped}
    return content


# The key an entity dict carries its OBSERVED appearance capacities on.
# Underscore-prefixed because nothing outside CQ is meant to write it: the
# extraction lane's only writer is inject_ownership_entities below, and
# both it and the sink filter the value to the vocabulary they honour.
# `speaker` is never accepted from here in any lane, because speaking is
# read off a transcript label and off nothing else. The residual risk of
# an entity arriving with a forged value is a person losing a `mention`
# they were owed, which costs nothing any read depends on.
ENTITY_CAPACITY_KEY = "_cq_capacities"


def inject_ownership_entities(
    content: dict,
    person_patch_type: str = "person",
    person_entity_type: str = "person",
    ownership_label: str = "owns",
    user_label: str | None = None,
) -> dict:
    """Give every person who OWNS something in this meeting an entity, so
    their presence in it is recorded.

    The hole this closes, found live on 2026-08-13 (origin
    866E8E1B-3586-42BE-B171-E7252CBA425E): the meeting ingested cleanly,
    produced seven patches including three commitments owned by two named
    people, and produced ZERO entities. `person_appearances` is written
    from the ENTITIES array, so neither person's presence moved. Two
    people owned work coming out of a meeting and, as far as the People
    surface could tell, were not in it.

    `enforce_person_ownership` is already the structural net for the
    model returning `value.owner` with no person patch behind it. It
    operates on `content["patches"]` and nothing did the equivalent for
    `content["entities"]`, so the net stopped one step short of presence.

    Reads the SURVIVING person patches rather than raw `value.owner`
    strings, which is why it sits late in the chain: by this point the
    enforcer has split compound owners into separate person patches,
    `enforce_owner_edge_agreement` has dropped ownership edges from
    bystanders, `strip_prose_from_person_names` has cut the sentence out
    of a name, and the self/placeholder gate has run. The names left are
    exactly the ones an entity should be keyed on. The same predicates
    are re-applied here anyway (compound split, placeholder and self
    filter, case-insensitive matching), because a model-emitted person
    patch can carry a compound name the enforcer never split.

    Capacity is `ownership`, never `speaker`. Owning an action item is
    not evidence of having spoken: work gets assigned to people in
    absentia, and the backfill's tier documentation makes the same
    distinction. It is not `mention` either for a name being introduced
    here, which is load bearing rather than pedantic: SS's duplicate veto
    reads the ownership-only-versus-speaker split off `capacities` to
    tell label drift from two humans, and stamping a mention CQ did not
    observe would erase that signature.

    Vocabulary comes from the app's manifest people block (doc 16 5.9),
    so an app that calls its people something else is served too.

    Idempotent: a second pass finds every name already in the entities
    array and injects nothing.

    Call AFTER drop_placeholder_and_self_person_patches and BEFORE
    drop_placeholder_entities, so the placeholder pass cleans up after
    this rather than before it. Mutates content in place.
    """
    patches = content.get("patches")
    if not isinstance(patches, list) or not patches:
        return content

    entities = content.get("entities")
    if not isinstance(entities, list):
        entities = []

    # Existing person entities, keyed case-insensitively. Only the person
    # entity type collides: "Marlowe" the org and "Marlowe" the person are
    # separate rows in the sink, so they are separate here too.
    by_name: dict[str, dict] = {}
    for ent in entities:
        if not isinstance(ent, dict) or ent.get("type") != person_entity_type:
            continue
        name = (ent.get("name") or "").strip()
        if name:
            by_name.setdefault(name.lower(), ent)

    injected: list[str] = []
    owners: list[str] = []

    for p in patches:
        if not isinstance(p, dict) or p.get("type") != person_patch_type:
            continue
        edges = p.get("connects_to")
        if not isinstance(edges, list):
            continue
        if not any(
            isinstance(c, dict) and c.get("label") == ownership_label for c in edges
        ):
            continue
        text = ((p.get("value") or {}).get("text") or "")
        for name in _split_compound_owner(text):
            if not _is_real_person_owner(name, user_label):
                continue
            name = name.strip()
            key = name.lower()
            existing = by_name.get(key)
            if existing is None:
                existing = {
                    "name": name,
                    "type": person_entity_type,
                    "description": "",
                    # Ownership ALONE. This name reached CQ as the owner
                    # of an item, which is not an observation that anyone
                    # said it out loud.
                    ENTITY_CAPACITY_KEY: [OWNERSHIP],
                }
                entities.append(existing)
                by_name[key] = existing
                injected.append(name)
            else:
                # Already in the array, so either the model listed them
                # (the mention WAS observed and ownership is additional)
                # or an earlier pass injected them (the marker says so and
                # is kept, which is what makes a second pass a no-op).
                # Anything else on the key is discarded: the vocabulary
                # here is mention and ownership, and `speaker` is a claim
                # about a transcript label that this function never makes.
                caps = [
                    c for c in (existing.get(ENTITY_CAPACITY_KEY) or [])
                    if c in (MENTION, OWNERSHIP)
                ] or [MENTION]
                if OWNERSHIP not in caps:
                    caps.append(OWNERSHIP)
                existing[ENTITY_CAPACITY_KEY] = caps
            owners.append(name)

    if not owners:
        return content

    content["entities"] = entities
    content["_ownership_entities_injected"] = {
        "injected": injected,
        "owners": sorted(set(owners)),
    }
    return content


def cap_entities(entities: object, cap: int) -> tuple:
    """Bound model-emitted entities without dropping injected ones.

    The same lesson the patch backstop learned the hard way: the cap
    exists to bound LLM-output noise, and applying it to a list that
    structural enforcement has appended to silently deletes the
    enforcement. An ownership-carrying entity is exempt, because dropping
    it takes a person's presence in the meeting with it.

    Returns `(kept, dropped_count)` and preserves order.
    """
    if not isinstance(entities, list):
        return [], 0
    if len(entities) <= cap:
        return entities, 0
    kept: list = []
    dropped = 0
    budget = max(0, cap)
    for ent in entities:
        caps = ent.get(ENTITY_CAPACITY_KEY) if isinstance(ent, dict) else None
        if caps and OWNERSHIP in caps:
            kept.append(ent)
        elif budget > 0:
            kept.append(ent)
            budget -= 1
        else:
            dropped += 1
    return kept, dropped


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


# ------------------------------------------------------------------
# Extraction patch backstop (2026-07-30 density probe, 12 real meetings)
# ------------------------------------------------------------------

def extraction_patch_backstop(
    transcript_chars: int, floor: int = 36, ceiling: int = 64
) -> int:
    """Length-scaled ceiling on patches stored per extraction.

    NOT a target — the model's own density judgment sets the count
    (measured: 1 patch on a sparse 28K-char meeting sitting next to 46
    on a dense 25K one; correlation length↔emission only 0.558). The
    backstop exists solely to bound degenerate enumeration, so it is
    sized never to bind legitimate content, with margin: the densest
    probed meeting emitted 46 at 25K chars (this returns 61 there) and
    47 at 50K (returns 64); the densest synthetic fixture emitted 32
    on a 1.7K-char transcript (returns 37).
    Length is the right variable for the BACKSTOP because it bounds
    what can physically be said in the time, even though it does not
    predict what was worth remembering.
    """
    scaled = 36 + max(0, int(transcript_chars)) // 1000
    return max(floor, min(ceiling, scaled))
