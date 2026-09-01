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


# ============================================================
# Cues section (associative retrieval)
# ============================================================


def test_cues_section_present_by_default(minimal_manifest):
    prompt = build_prompt(minimal_manifest)
    assert "=== CUES: associative retrieval hooks ===" in prompt
    assert "`value.cues`" in prompt


def test_cues_schema_field_in_output_schema(minimal_manifest):
    schema = build_output_schema(minimal_manifest)
    value_props = schema["properties"]["patches"]["items"]["properties"]["value"]["properties"]
    assert value_props["cues"]["type"] == "array"
    assert value_props["cues"]["maxItems"] == 5


def test_cues_disabled_via_manifest(minimal_manifest):
    m = copy.deepcopy(minimal_manifest)
    m["extraction_prompt_guidance"] = {"cues_enabled": False}
    prompt = build_prompt(m)
    assert "CUES — associative retrieval hooks" not in prompt


def test_cue_guidance_override_and_per_type_lines(minimal_manifest):
    m = copy.deepcopy(minimal_manifest)
    m["extraction_prompt_guidance"] = {"cue_guidance": "Emit rehearsal skill topics only."}
    m["patch_types"][0]["cue_guidance"] = "Use the skill name being coached."
    prompt = build_prompt(m)
    assert "Emit rehearsal skill topics only." in prompt
    assert "- **note**: Use the skill name being coached." in prompt
    # Default guidance body replaced, section header retained
    assert "`value.cues` is how a patch gets FOUND" not in prompt


# ============================================================
# Universal value fields merged into every rendered type shape
# (2026-07-30 cue-starvation root cause: manifest value_shape
# declarations predate cues/salience/deadline_date, and models obey
# the per-type shape — the most concrete spec — over the generic
# sections. 0% cue emission on the generated prompt vs 85% on a
# cue-bearing shape, reproduced on two models.)
# ============================================================


def test_rendered_shape_includes_universal_fields(minimal_manifest):
    prompt = build_prompt(minimal_manifest)
    shape_line = next(l for l in prompt.splitlines() if "Value shape:" in l)
    for field in ("cues", "salience", "deadline_date"):
        assert field in shape_line, f"{field} missing from rendered shape"
    # manifest-declared field survives untouched
    assert "text: string" in shape_line


def test_manifest_declared_fields_win_on_conflict(minimal_manifest):
    m = copy.deepcopy(minimal_manifest)
    m["patch_types"][0]["value_shape"]["cues"] = "custom-spec"
    prompt = build_prompt(m)
    shape_line = next(l for l in prompt.splitlines() if "Value shape:" in l)
    assert "cues: custom-spec" in shape_line
    assert "see CUES section" not in shape_line.split("cues:")[1].split(",")[0]


def test_shape_hints_reference_existing_sections(minimal_manifest):
    # The shape's "see CUES/SALIENCE section" pointers must not dangle.
    prompt = build_prompt(minimal_manifest)
    assert "CUES" in prompt and "SALIENCE" in prompt


def test_killed_sections_not_advertised_in_shapes(minimal_manifest):
    m = copy.deepcopy(minimal_manifest)
    m["extraction_prompt_guidance"] = {"cues_enabled": False, "salience_enabled": False}
    prompt = build_prompt(m)
    shape_line = next(l for l in prompt.splitlines() if "Value shape:" in l)
    assert "cues" not in shape_line
    assert "salience" not in shape_line
    assert "deadline_date" in shape_line  # no kill switch for deadlines


# ============================================================
# Entity description: observed, never inferred (2026-08-21)
# ============================================================
#
# Steven Williams was served as "Immigration attorney" because he spent
# a meeting discussing immigration tooling and privilege; he is not one.
# The model described the shape of the conversation and called it the
# person. The rule lives in the DEFAULT entity guidance, which is what
# every app without its own `entity_guidance` renders.

_DESC_RULE_MARKER = "- `description` is what is durably TRUE OF THIS PERSON"


def test_description_rule_renders_for_ss_manifest():
    """The marker moved on 2026-08-31 when the rule was rewritten.

    The old rule asked for "what this transcript SHOWS" and told the
    model to describe the conduct where no role was stated, which
    produced descriptions of a Tuesday and meant 0 of 122 rows were ever
    confirmed as the same perception. The anti-inference contrast pair
    below is unchanged and is the half that must never be lost.
    """
    import json
    from pathlib import Path

    fixture_path = (
        Path(__file__).resolve().parent.parent.parent
        / "init-db"
        / "11_shouldersurf_schema.json"
    )
    if not fixture_path.exists():
        pytest.skip("SS manifest fixture not found")
    with open(fixture_path) as f:
        manifest = json.load(f)
    # SS declares no entity_guidance override, so the default rules (and
    # this one) are what its extractions see. If SS ever adds an
    # override, this test is the reminder to carry the rule across.
    assert "entity_guidance" not in (manifest.get("extraction_prompt_guidance") or {})
    prompt = build_prompt(manifest)
    assert _DESC_RULE_MARKER in prompt
    # The contrast pair is the part that moves a model (doc 19.8), so it
    # must survive any future trimming of the rule text.
    assert "NOT thereby an immigration attorney" in prompt


def test_description_rule_is_part_of_default_guidance_not_bolted_on(minimal_manifest):
    # A manifest that supplies its own entity_guidance REPLACES the
    # default block. The rule must vanish with it, which proves the
    # assertion above is testing the default guidance path and not some
    # unconditional string the builder always emits.
    m = copy.deepcopy(minimal_manifest)
    m.setdefault("extraction_prompt_guidance", {})["entity_guidance"] = "RULES: custom."
    prompt = build_prompt(m)
    assert "RULES: custom." in prompt
    assert _DESC_RULE_MARKER not in prompt


class TestEdgeShapeIsInThePrompt:
    """The edge contract lived ONLY in the JSON schema, which the
    Anthropic client accepts for interface parity and does not put on
    the wire. So the model never saw it, invented `{"type": ...,
    "target": ...}`, and every edge was discarded twice over:
    `enforce_connection_vocabulary` drops an edge with no `label`, and
    `store_connected_patches` skips one with no `target_text`.

    Measured before the fix: 22 edges emitted, 22 dropped, 0 labels.
    After: 32 kept, 0 dropped, and `owed_to` produced 5 edges in two
    runs against 2 in all of production history.

    This is the June entity regression in a different field, and these
    tests exist so it cannot come back a third time.
    """

    def _prompt(self):
        import json
        from pathlib import Path
        manifest = json.loads(
            (Path(__file__).resolve().parents[2]
             / "init-db" / "11_shouldersurf_schema.json").read_text())
        return build_prompt(manifest)

    def test_the_three_edge_keys_are_named_in_the_prompt(self):
        p = self._prompt()
        for key in ("label", "target_text", "target_type"):
            assert f'"{key}"' in p, f"{key} must be stated in the prompt"

    def test_the_wrong_keys_are_named_as_wrong(self):
        # Telling a model what not to write is what stops it reaching
        # for the plausible-looking pair it reached for before.
        p = self._prompt()
        assert "`type` or `target` as keys on an edge" in p

    def test_target_text_is_described_as_the_resolution_key(self):
        # An edge whose target_text matches no patch is dropped, so the
        # model has to know the string is load bearing rather than
        # descriptive.
        assert "resolved" in self._prompt()

    def test_the_shape_sits_with_the_labels_not_somewhere_else(self):
        # It has to be where the model is choosing a label, not 200
        # lines away in the output-shape preamble.
        p = self._prompt()
        assert p.index("EXACTLY these keys") > p.index("CONNECTION LABELS")
        # The BULLET form, because `belongs_to` also appears earlier
        # inside the deliverable type guidance.
        assert p.index("EXACTLY these keys") < p.index("- `belongs_to`")
