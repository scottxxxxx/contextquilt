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
    sections.append(_reasoning_requirement(guidance))
    sections.append(_output_shape(manifest))
    sections.append(_patch_types_section(manifest))
    sections.append(_connection_labels_section(manifest))
    sections.append(_priority_order(guidance))
    sections.append(_hard_caps(guidance))
    sections.append(_exclusion_examples(guidance))
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
        "required": ["patches", "entities", "relationships"],
        "properties": {
            "_reasoning": {"type": "string"},
            "you_speaker_present": {"type": "boolean"},
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


def _output_shape(manifest: Dict[str, Any]) -> str:
    return (
        "=== OUTPUT SHAPE ===\n"
        "Return a JSON object with exactly these keys:\n"
        "- `_reasoning`: short scratchpad explaining why you chose the patches you did\n"
        "- `patches`: array of typed patches (see PATCH TYPES below)\n"
        "- `entities`: array of named things (for the recall name index)\n"
        "- `relationships`: array of edges between entities\n"
        "\n"
        "Each patch has: `type` (one of the domain types), `value` (object with "
        "`text` and optional `owner` / `deadline`), and optional `connects_to` "
        "array of edges to other patches in this same output."
    )


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
            shape_fields = ", ".join(
                f"{k}: {v}" for k, v in shape.items()
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
