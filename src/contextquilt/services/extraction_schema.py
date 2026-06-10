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
                        "required": ["text", "owner", "deadline", "deadline_date"],
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
_DEADLINE_PAST_WINDOW = timedelta(days=730)
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


def strip_owner_on_self_typed_patches(content: dict) -> dict:
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
    """
    for patch in content.get("patches") or []:
        if patch.get("type") not in _SELF_TYPED_PATCH_TYPES_WITH_IMPLICIT_OWNER:
            continue
        value = patch.get("value")
        if isinstance(value, dict) and value.get("owner"):
            value["owner"] = None
    return content


# Tokens that, when they appear with spaces around them inside a
# person-patch value.text, mark the boundary between the name and a
# trailing description the LLM should not have included. The order
# doesn't matter (we use the earliest match), but the set is kept
# conservative so multi-word names like "Bob Martinez" survive.
#
# Notably absent: " and " (occurs in "Anand and Family"-style legit
# names — handled by split_compound_person_patches), bare hyphens
# without surrounding spaces (preserves "Jean-Luc", "O'Brien-Smith"),
# and " of " (preserves "Catherine of Aragon"-style; not common in our
# data but cheap to keep).
_PERSON_NAME_PROSE_SEPARATORS = (
    " — ",   # em-dash with spaces (the worst offender — prompt example used this)
    " – ",   # en-dash with spaces
    " - ",   # ASCII hyphen with spaces ("Rahul - technical lead and primary presenter")
    ", ",    # ("Speaker 5, AI tool operator and ...")
    " is ",  # ("Santosh is a developer working for ...")
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
    field (``"Christina - customer success point of contact for ..."``)
    when the prompt asked for a name. The dashboard then renders that
    sentence as the person's display name and the entity index can't
    match it against future mentions of just "Christina".

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


def _split_compound_owner(owner_text: str | None) -> list[str]:
    """Split a slash-joined owner string into individual name parts.

    The LLM occasionally emits joint owners like ``"Srikanth/Ella"`` or
    ``"Sai/Santosh"`` when a transcript line says "Srikanth and Ella will
    handle it." Without splitting, the enforcer creates one synthetic
    person patch with the literal compound text and a single owns edge —
    losing the per-person attribution.

    Conservative splitter: ``/`` only. We don't split on ``,``, ``&``,
    or ``" and "`` — those legitimately appear inside single names
    ("Smith, John", "AT&T", "Anand and family") and over-splitting would
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
    Action items routinely come back with `value.owner: "Brian"` and no
    Brian person patch and no `owns` connection. This is the structural
    safety net — same shape as enforce_connection_requirements for parents.

    For each commitment/blocker/decision/goal in content["patches"]:
      1. Read value.owner.
      2. Split slash-joined compound owners ("Srikanth/Ella") into
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
        # Split compound owners ("Srikanth/Ella") into individual names
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
