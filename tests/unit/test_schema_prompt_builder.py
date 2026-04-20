"""Unit tests for the schema-driven extraction prompt builder."""

import copy

import pytest

from src.contextquilt.services.schema_prompt_builder import (
    build_prompt,
    build_output_schema,
)


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def minimal_manifest():
    return {
        "app_id": "test-app",
        "display_name": "Test App",
        "version": 1,
        "facet_enum_version": 1,
        "patch_types": [
            {
                "domain_type": "note",
                "facet": "Episode",
                "permanence": "week",
                "display_name": "Note",
                "description": "A freeform observation worth remembering.",
                "value_shape": {"text": "string"},
            }
        ],
        "connection_labels": [
            {
                "label": "mentions",
                "role": "informs",
                "from_types": ["note"],
                "to_types": ["note"],
                "description": "One note mentions another.",
            }
        ],
    }


@pytest.fixture
def manifest_with_guidance(minimal_manifest):
    m = copy.deepcopy(minimal_manifest)
    m["extraction_prompt_guidance"] = {
        "role_context": "You are an extractor for widget meetings.",
        "speaker_conventions": "Speakers are labeled in brackets.",
        "reasoning_requirement": "Include a _reasoning scratchpad.",
        "priority_order": ["Notes first", "Then everything else"],
        "hard_caps": {
            "total_patches_per_meeting": 10,
            "entities_per_meeting": 8,
            "relationships_per_meeting": 5,
            "per_type_caps": {"note": 3},
        },
        "exclusion_examples": [
            "Scheduling logistics",
            "Procedural chatter",
        ],
    }
    return m


@pytest.fixture
def manifest_with_override(minimal_manifest):
    m = copy.deepcopy(minimal_manifest)
    m["extraction_prompt_override"] = "VERBATIM PROMPT: extract widgets."
    return m


# ============================================================
# build_prompt
# ============================================================


def test_override_is_returned_verbatim(manifest_with_override):
    prompt = build_prompt(manifest_with_override)
    assert prompt == "VERBATIM PROMPT: extract widgets."


def test_generated_prompt_mentions_domain_type(minimal_manifest):
    prompt = build_prompt(minimal_manifest)
    assert "note" in prompt
    assert "Episode" in prompt
    assert "week" in prompt


def test_generated_prompt_mentions_connection_label(minimal_manifest):
    prompt = build_prompt(minimal_manifest)
    assert "mentions" in prompt
    assert "informs" in prompt


def test_guidance_sections_included(manifest_with_guidance):
    prompt = build_prompt(manifest_with_guidance)
    assert "widget meetings" in prompt  # role_context
    assert "Speakers are labeled" in prompt  # speaker_conventions
    assert "_reasoning scratchpad" in prompt  # reasoning_requirement
    assert "Notes first" in prompt  # priority_order
    assert "Maximum 10 patches" in prompt  # hard_caps
    assert "Maximum 3 patches of type `note`" in prompt  # per_type_caps
    assert "Scheduling logistics" in prompt  # exclusion_examples


def test_missing_guidance_still_produces_prompt(minimal_manifest):
    """Even without any guidance keys, the prompt should still be coherent."""
    prompt = build_prompt(minimal_manifest)
    assert "ContextQuilt" in prompt
    assert "PATCH TYPES" in prompt
    assert "CONNECTION LABELS" in prompt
    assert "OUTPUT SHAPE" in prompt


def test_empty_string_override_falls_back_to_generated(minimal_manifest):
    m = copy.deepcopy(minimal_manifest)
    m["extraction_prompt_override"] = "   "
    prompt = build_prompt(m)
    # Should NOT be the whitespace override; should be the generated one
    assert "PATCH TYPES" in prompt


# ============================================================
# build_output_schema
# ============================================================


def test_output_schema_enum_matches_declared_types(minimal_manifest):
    schema = build_output_schema(minimal_manifest)
    patch_type_enum = (
        schema["properties"]["patches"]["items"]["properties"]["type"]["enum"]
    )
    assert patch_type_enum == ["note"]


def test_output_schema_connection_label_enum(minimal_manifest):
    schema = build_output_schema(minimal_manifest)
    label_enum = (
        schema["properties"]["patches"]["items"]["properties"]
        ["connects_to"]["items"]["properties"]["label"]["enum"]
    )
    assert label_enum == ["mentions"]


def test_output_schema_role_enum_always_three(minimal_manifest):
    schema = build_output_schema(minimal_manifest)
    role_enum = (
        schema["properties"]["patches"]["items"]["properties"]
        ["connects_to"]["items"]["properties"]["role"]["enum"]
    )
    assert set(role_enum) == {"parent", "depends_on", "informs"}


def test_output_schema_entity_types_fallback_when_not_declared(minimal_manifest):
    schema = build_output_schema(minimal_manifest)
    entity_type_enum = (
        schema["properties"]["entities"]["items"]["properties"]["type"]["enum"]
    )
    assert "person" in entity_type_enum
    assert "project" in entity_type_enum


def test_output_schema_entity_types_honors_manifest(minimal_manifest):
    m = copy.deepcopy(minimal_manifest)
    m["entity_types"] = [
        {"entity_type": "widget", "display_name": "Widget", "description": "A widget."}
    ]
    schema = build_output_schema(m)
    entity_type_enum = (
        schema["properties"]["entities"]["items"]["properties"]["type"]["enum"]
    )
    assert entity_type_enum == ["widget"]


# ============================================================
# SS manifest smoke
# ============================================================


def test_shouldersurf_manifest_generates_coherent_prompt():
    """Smoke test: the SS manifest shipped in init-db/ produces a sensible prompt."""
    import json
    from pathlib import Path

    fixture_path = (
        Path(__file__).resolve().parent.parent.parent
        / "init-db"
        / "11_shouldersurf_schema.json"
    )
    if not fixture_path.exists():
        pytest.skip(f"SS manifest fixture not found at {fixture_path}")
    with open(fixture_path) as f:
        manifest = json.load(f)

    prompt = build_prompt(manifest)
    schema = build_output_schema(manifest)

    # Sanity check: prompt mentions every type declared in the manifest
    for patch_type in manifest["patch_types"]:
        assert patch_type["domain_type"] in prompt, (
            f"SS manifest type {patch_type['domain_type']!r} missing from generated prompt"
        )

    # Sanity check: schema enum matches the manifest's declared types
    patch_type_enum = (
        schema["properties"]["patches"]["items"]["properties"]["type"]["enum"]
    )
    assert len(patch_type_enum) == len(manifest["patch_types"])
