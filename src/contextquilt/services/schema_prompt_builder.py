"""
Schema-driven extraction prompt builder.

Generates an extraction system prompt and JSON output schema from a
registered app manifest. This replaces hand-maintained per-app prompts
with prompts auto-generated from the app's declared schema.

Apps with mature, hand-tuned prompts can supply
`extraction_prompt_override` in their manifest and CQ will use it
verbatim. Apps without override get a generated prompt from the
structural declarations (patch_types, connection_labels,
extraction_prompt_guidance).

See docs/design/app-schema-registration.md for the manifest shape.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


# ============================================================
# Top-level API
# ============================================================


def build_prompt(manifest: Dict[str, Any]) -> str:
    """Return the system prompt string for this app's extraction.

    If the manifest provides `extraction_prompt_override`, returns it
    verbatim. Otherwise synthesizes a prompt from the structural
    declarations plus `extraction_prompt_guidance`.
    """
    override = manifest.get("extraction_prompt_override")
    if isinstance(override, str) and override.strip():
        return override

    guidance = manifest.get("extraction_prompt_guidance") or {}

    sections: List[str] = []
    sections.append(_preamble(manifest, guidance))
    sections.append(_speaker_conventions(guidance))
    sections.append(_language_section())
    sections.append(_reasoning_requirement(guidance))
    sections.append(_output_shape(manifest))
    sections.append(_cues_section(manifest, guidance))
    sections.append(_salience_section(guidance))
    sections.append(_patch_types_section(manifest))
    sections.append(_connection_labels_section(manifest))
    sections.append(_priority_order(guidance))
    sections.append(_hard_caps(guidance))
    sections.append(_exclusion_examples(guidance))
    sections.append(_resolved_commitments_section())
    sections.append(_closing_rules())

    # Drop any empty sections before joining
    return "\n\n".join(s for s in sections if s and s.strip())


def build_output_schema(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Return a JSON schema describing the expected extraction output.

    The schema is derived from the registered patch types and connection
    labels. Used by structured-output-capable LLM providers
    (OpenAI json_schema, Gemini, etc.) to constrain decoding.
    """
    patch_type_enum = [pt["domain_type"] for pt in manifest.get("patch_types", [])]
    label_enum = [lb["label"] for lb in manifest.get("connection_labels", [])]
    role_enum = ["parent", "depends_on", "informs"]

    entity_types = manifest.get("entity_types", []) or []
    entity_type_enum = [et["entity_type"] for et in entity_types] or [
        "person", "project", "company", "feature", "artifact", "deadline", "metric"
    ]

    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["output_language", "patches", "resolved_commitments", "entities", "relationships"],
        "properties": {
            "you_speaker_present": {"type": "boolean"},
            # Language commitment — generated before patches (property order
            # drives generation order under strict mode) so English context
            # blocks can't pull the output prose back to English.
            "output_language": {"type": "string"},
            "_reasoning": {"type": "string"},
            "patches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["type", "value"],
                    "properties": {
                        "type": {"type": "string", "enum": patch_type_enum or [""]},
                        "value": {
                            "type": "object",
                            "additionalProperties": True,
                            "properties": {
                                "text": {"type": "string"},
                                "owner": {"type": ["string", "null"]},
                                "deadline": {"type": ["string", "null"]},
                                "deadline_date": {"type": ["string", "null"]},
                                "cues": {
                                    "type": "array",
                                    "maxItems": 5,
                                    "items": {"type": "string"},
                                },
                                "salience": {"type": ["string", "null"]},
                            },
                        },
                        "connects_to": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["target_text", "target_type", "role"],
                                "properties": {
                                    "target_text": {"type": "string"},
                                    "target_type": {"type": "string", "enum": patch_type_enum or [""]},
                                    "role": {"type": "string", "enum": role_enum},
                                    "label": {"type": "string", "enum": label_enum or [""]},
                                },
                            },
                        },
                    },
                },
            },
            "resolved_commitments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["patch_id", "evidence"],
                    "properties": {
                        "patch_id": {"type": "string"},
                        "evidence": {"type": "string"},
                    },
                },
            },
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "type"],
                    "properties": {
                        "name": {"type": "string"},
                        "type": {"type": "string", "enum": entity_type_enum},
                        "description": {"type": ["string", "null"]},
                    },
                },
            },
            "relationships": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["from", "to", "type"],
                    "properties": {
                        "from": {"type": "string"},
                        "to": {"type": "string"},
                        "type": {"type": "string"},
                        "context": {"type": ["string", "null"]},
                    },
                },
            },
        },
    }


# ============================================================
# Section builders
# ============================================================


def _preamble(manifest: Dict[str, Any], guidance: Dict[str, Any]) -> str:
    role_context = guidance.get("role_context") or (
        f"You are a structured data extraction engine for ContextQuilt, a persistent "
        f"memory system. You are extracting typed memory patches for the app "
        f"{manifest.get('display_name', manifest.get('app_id'))!r}."
    )
    return role_context


def _speaker_conventions(guidance: Dict[str, Any]) -> str:
    conv = guidance.get("speaker_conventions")
    if not conv:
        return ""
    return f"=== SPEAKER CONVENTIONS ===\n{conv}"


def _reasoning_requirement(guidance: Dict[str, Any]) -> str:
    req = guidance.get("reasoning_requirement")
    if not req:
        return ""
    return f"=== REASONING REQUIREMENT ===\n{req}"


def _language_section() -> str:
    return (
        "=== LANGUAGE ===\n"
        "Transcripts may be in ANY language, or a mix (e.g. one speaker in "
        "Spanish, another in English). Extract with EQUAL diligence from "
        "every language present — a trait, preference, person, commitment, "
        "or blocker stated in Spanish, Japanese, or Portuguese is exactly "
        "as memorable as one stated in English. Never skip a speaker's "
        "content because of the language they spoke.\n"
        "\n"
        "Write all output prose — patch value `text`, entity `description`, "
        "relationship `context` — in the user's language: use the "
        "`User language:` line at the top of the input if present "
        "(e.g. \"User language: es\"); otherwise use the dominant language "
        "spoken by the (you) speaker. Commit to it in the `output_language` "
        "field BEFORE generating patches, and honor it for every prose field "
        "after — these instructions and any context blocks being in English "
        "does NOT change the output language. Keep proper names verbatim as spoken. "
        "Structural fields are language-independent and unchanged: patch "
        "`type`, connection roles/labels, entity `type`, and `deadline_date` "
        "(always YYYY-MM-DD). The `deadline` field stays as spoken, in its "
        "original language."
    )


def _output_shape(manifest: Dict[str, Any]) -> str:
    return (
        "=== OUTPUT SHAPE ===\n"
        "Return a JSON object with exactly these keys:\n"
        "- `output_language`: the language code ALL output prose must be written in "
        "(from the `User language:` line, else the (you) speaker's dominant language) — "
        "set this before generating patches and honor it for every prose field\n"
        "- `_reasoning`: short scratchpad explaining why you chose the patches you did\n"
        "- `patches`: array of typed patches (see PATCH TYPES below)\n"
        "- `resolved_commitments`: array of prior open commitments this transcript shows as completed (see RESOLVED COMMITMENTS section)\n"
        "- `entities`: array of named things (for the recall name index)\n"
        "- `relationships`: array of edges between entities\n"
        "\n"
        "Each patch has: `type` (one of the domain types), `value` (object with "
        "`text` and optional `owner` / `deadline` / `deadline_date` / `cues`), and optional "
        "`connects_to` array of edges to other patches in this same output.\n"
        "\n"
        "When a patch has a deadline, set `deadline` to the deadline as spoken "
        "(\"tomorrow\", \"end of week\", \"June 19th\") AND set `deadline_date` to "
        "that deadline resolved to an absolute calendar date in YYYY-MM-DD form. "
        "Resolve relative expressions against the `Meeting date:` line at the top "
        "of the input — e.g. if the meeting date is 2026-06-10, \"tomorrow\" → "
        "\"2026-06-11\" and \"end of week\" → the upcoming Friday. If the deadline "
        "cannot be tied to a specific date (\"after the board meeting\", \"soon\"), "
        "set `deadline_date` to null. Never guess a year — when no Meeting date "
        "line is present and the deadline is relative, set `deadline_date` to null. "
        "This applies to every patch type that carries a date, not just "
        "action-like types — a goal with a target date gets both fields the "
        "same way."
    )


def _cues_section(manifest: Dict[str, Any], guidance: Dict[str, Any]) -> str:
    """Associative-retrieval cue instruction.

    Generic by default; apps tune it from the manifest without CQ code
    changes: `guidance.cue_guidance` replaces the default guidance prose,
    and a patch_types entry may carry its own `cue_guidance` line
    (rendered per type). `guidance.cues_enabled: false` drops the
    section entirely (and with it, cue emission).
    """
    if guidance.get("cues_enabled") is False:
        return ""
    body = guidance.get("cue_guidance") or (
        "`value.cues` is how a patch gets FOUND later when nobody says an "
        "entity name. Ask: \"in a future conversation, what topic words "
        "would someone use when this patch should surface?\" Emit those, "
        "0-5 per patch: short lowercase phrases of 1-4 words "
        "(\"pricing model\", \"visa paperwork\") — topics, not sentences. "
        "Do NOT repeat entity names (the entities array indexes those), "
        "and do NOT emit medium words (\"meeting\", \"update\") or "
        "anything generic enough to match every conversation. An empty "
        "array is correct when the entities already cover it."
    )
    lines = ["=== CUES — associative retrieval hooks ===", body]
    per_type = [
        f"- **{pt.get('domain_type')}**: {pt['cue_guidance']}"
        for pt in (manifest.get("patch_types") or [])
        if isinstance(pt, dict) and pt.get("cue_guidance")
    ]
    if per_type:
        lines.append("")
        lines.append("Type-specific cue guidance:")
        lines.extend(per_type)
    return "\n".join(lines)


def _salience_section(guidance: Dict[str, Any]) -> str:
    """Judgment-weighted encoding instruction.

    Manifest hooks (no CQ code): `guidance.salience_guidance` replaces the
    default prose; `guidance.salience_enabled: false` drops the section
    (and with it, salience emission).
    """
    if guidance.get("salience_enabled") is False:
        return ""
    body = guidance.get("salience_guidance") or (
        "`value.salience` weights how long a memory lives and how eagerly "
        "it resurfaces. Set it from what the SPEAKER signaled: \"high\" "
        "ONLY for unusual weight (emotional emphasis, surprise, explicit "
        "stakes, a reversal of something previously believed, repetition); "
        "\"low\" for passing remarks unlikely to matter later; null for "
        "everything else — MOST patches are null."
    )
    return "=== SALIENCE — how strongly to remember ===\n" + body


def _patch_types_section(manifest: Dict[str, Any]) -> str:
    patch_types = manifest.get("patch_types") or []
    if not patch_types:
        return ""

    lines = ["=== PATCH TYPES — use the most specific type that fits ===", ""]
    for pt in patch_types:
        lines.append(f"- **{pt.get('domain_type')}** (facet: {pt.get('facet')}, permanence: {pt.get('permanence')})")
        desc = pt.get("description")
        if desc:
            lines.append(f"    {desc}")
        shape = pt.get("value_shape")
        if isinstance(shape, dict):
            # Merge the universal optional fields into every rendered
            # shape. Manifest value_shape declarations predate cues /
            # salience / deadline_date, and models obey the per-type
            # shape (the most concrete spec) over the generic sections
            # above it — 14 shapes without `cues` beat one CUES section
            # every time. Root cause of the 2026-07-30 cue-starvation
            # finding: 0% cue emission on the generated prompt vs 85%
            # on a prompt whose shape includes cues, on two different
            # models. Manifest-declared fields always win on conflict.
            guidance = manifest.get("extraction_prompt_guidance") or {}
            universal = [
                ("deadline", "string?"),
                ("deadline_date", "string?"),
            ]
            # A killed section must not be advertised by the shapes —
            # the field only merges when its instruction section renders.
            if guidance.get("cues_enabled") is not False:
                universal.append(("cues", "string[]? (0-5, see CUES section)"))
            if guidance.get("salience_enabled") is not False:
                universal.append(("salience", "string? (high|low, see SALIENCE section)"))
            merged = dict(shape)
            for field, spec in universal:
                merged.setdefault(field, spec)
            shape_fields = ", ".join(
                f"{k}: {v}" for k, v in merged.items()
            )
            lines.append(f"    Value shape: {{{shape_fields}}}")
        rules = pt.get("extraction_rules") or {}
        rules_guidance = rules.get("guidance")
        if rules_guidance:
            lines.append(f"    When to emit: {rules_guidance}")
        if pt.get("self_only"):
            lines.append("    Applies ONLY to the submitting user.")
        if pt.get("completable"):
            lines.append("    Can be marked completed.")
        if pt.get("project_scoped"):
            lines.append("    Project-scoped — should connect to a project patch via belongs_to.")
        lines.append("")
    return "\n".join(lines).rstrip()


def _connection_labels_section(manifest: Dict[str, Any]) -> str:
    labels = manifest.get("connection_labels") or []
    if not labels:
        return ""

    lines = ["=== CONNECTION LABELS — valid `connects_to` edges ===", ""]
    for lb in labels:
        label = lb.get("label")
        role = lb.get("role")
        from_types = lb.get("from_types") or []
        to_types = lb.get("to_types") or []
        desc = lb.get("description", "")
        lines.append(
            f"- `{label}` (role: {role}): "
            f"{', '.join(from_types)} → {', '.join(to_types)}"
        )
        if desc:
            lines.append(f"    {desc}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _priority_order(guidance: Dict[str, Any]) -> str:
    order = guidance.get("priority_order")
    if not order:
        return ""
    lines = ["=== PRIORITY ORDER (when you must choose within the patch budget) ===", ""]
    for i, item in enumerate(order, 1):
        lines.append(f"{i}. {item}")
    return "\n".join(lines)


def _hard_caps(guidance: Dict[str, Any]) -> str:
    caps = guidance.get("hard_caps") or {}
    if not caps:
        return ""
    lines = ["=== HARD CAPS ==="]
    total = caps.get("total_patches_per_meeting") or caps.get("total_patches")
    if total:
        lines.append(f"- Maximum {total} patches total per input.")
    entities = caps.get("entities_per_meeting") or caps.get("entities")
    if entities:
        lines.append(f"- Maximum {entities} entities.")
    rels = caps.get("relationships_per_meeting") or caps.get("relationships")
    if rels:
        lines.append(f"- Maximum {rels} relationships.")
    per_type = caps.get("per_type_caps") or {}
    for domain_type, cap in per_type.items():
        lines.append(f"- Maximum {cap} patches of type `{domain_type}`.")
    return "\n".join(lines) if len(lines) > 1 else ""


def _exclusion_examples(guidance: Dict[str, Any]) -> str:
    excl = guidance.get("exclusion_examples")
    if not excl:
        return ""
    lines = ["=== DO NOT EXTRACT ==="]
    for item in excl:
        lines.append(f"- {item}")
    return "\n".join(lines)


def _resolved_commitments_section() -> str:
    """Universal section, included for every manifest-generated prompt.

    The worker injects an `Open commitments` block into user_content when
    the user has any open commitment patches. This section tells the LLM
    how to use that block and what to emit in the `resolved_commitments`
    output field.
    """
    return (
        "=== RESOLVED COMMITMENTS ===\n"
        "If user_content begins with an `Open commitments` block, those are\n"
        "prior commitments the user already made that are still open in\n"
        "their memory. Your job is to detect when THIS transcript indicates\n"
        "any of them are now done, and report those patch_ids back in\n"
        "`resolved_commitments`.\n"
        "\n"
        "Trigger phrases to match generously (not exhaustive):\n"
        "  - \"I sent the email to <person>\"\n"
        "  - \"we shipped <thing>\"\n"
        "  - \"I finished <doc/PR/draft>\"\n"
        "  - \"scheduled the call with <person>\"\n"
        "  - \"<thing> is done / live / merged / handed off\"\n"
        "  - \"got back to <person>\"\n"
        "  - \"deleted / archived / closed <thing>\"\n"
        "\n"
        "Rules:\n"
        "1. Only include patch_ids that appear in the `Open commitments` block.\n"
        "   Never invent or guess — the worker rejects unknown patch_ids.\n"
        "2. Copy patch_id strings verbatim, character for character.\n"
        "3. The `evidence` field is a short quote or paraphrase from the\n"
        "   transcript showing the action was completed. Under ~300 chars.\n"
        "4. If the transcript doesn't reference any open commitment, emit\n"
        "   an empty array. Do NOT force matches.\n"
        "5. If no `Open commitments` block is present, always emit an empty array.\n"
        "6. Match liberally on the substance of the action, not the surface\n"
        "   wording. \"Got back to Ulster\" resolves \"Email Ulster about the\n"
        "   contract\" if both clearly refer to the same conversation."
    )


def _closing_rules() -> str:
    return (
        "=== GENERAL RULES ===\n"
        "1. Every value must be grounded in the transcript — do not invent.\n"
        "2. Entity names must match exactly as mentioned in the transcript.\n"
        "3. Keep each patch's text concise (one clear sentence).\n"
        "4. If a section has nothing to extract, return an empty array.\n"
        "5. Only create connections that genuinely exist in the transcript.\n"
        "6. Prefer consolidation — one commitment patch over three sub-tasks."
    )
