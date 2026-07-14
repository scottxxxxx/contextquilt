"""Unit tests for consolidation pure logic + manifest rule validation."""

import copy

from src.contextquilt.services.consolidation import (
    DEFAULT_MIN_PATCHES,
    MAX_SOURCE_TEXTS,
    build_synthesis_content,
    parse_consolidation_rules,
    parse_synthesis_response,
)
from src.contextquilt.services.schema_validator import validate_manifest

MANIFEST = {
    "app_id": "t", "version": 1, "facet_enum_version": 1,
    "patch_types": [
        {"domain_type": "takeaway", "facet": "Episode", "permanence": "week",
         "display_name": "T", "description": "d", "value_shape": {"text": "string"}},
        {"domain_type": "trait", "facet": "Attribute", "permanence": "year",
         "display_name": "Tr", "description": "d", "value_shape": {"text": "string"}},
    ],
    "connection_labels": [
        {"label": "mentions", "role": "informs", "from_types": ["takeaway"],
         "to_types": ["trait"], "description": "d"},
    ],
}


# ------------------------------------------------------------------
# parse_consolidation_rules
# ------------------------------------------------------------------

def test_parses_well_formed_rule_and_defaults_min_patches():
    m = copy.deepcopy(MANIFEST)
    m["consolidation_rules"] = [
        {"from_types": ["takeaway"], "produce_type": "trait"},
        {"from_types": ["takeaway"], "produce_type": "trait", "min_patches": 5,
         "guidance": "Only stable habits."},
    ]
    rules = parse_consolidation_rules(m)
    assert len(rules) == 2
    assert rules[0]["min_patches"] == DEFAULT_MIN_PATCHES
    assert rules[1]["min_patches"] == 5
    assert rules[1]["guidance"] == "Only stable habits."


def test_drops_rules_referencing_undeclared_types_or_junk():
    m = copy.deepcopy(MANIFEST)
    m["consolidation_rules"] = [
        {"from_types": ["ghost"], "produce_type": "trait"},
        {"from_types": ["takeaway"], "produce_type": "ghost"},
        {"from_types": "takeaway", "produce_type": "trait"},
        "not a dict",
        {"from_types": ["takeaway"], "produce_type": "trait", "min_patches": 1},
    ]
    rules = parse_consolidation_rules(m)
    # only the last survives, with min_patches clamped to the default
    assert len(rules) == 1
    assert rules[0]["min_patches"] == DEFAULT_MIN_PATCHES


def test_no_rules_key_or_no_manifest_yields_empty():
    assert parse_consolidation_rules(MANIFEST) == []
    assert parse_consolidation_rules(None) == []


# ------------------------------------------------------------------
# synthesis content + response parsing
# ------------------------------------------------------------------

def test_synthesis_content_caps_sources_and_includes_guidance():
    texts = [f"obs {i}" for i in range(20)]
    content = build_synthesis_content("pricing model", "trait", texts, "Be strict.")
    assert "Topic (shared cue): pricing model" in content
    assert "App guidance: Be strict." in content
    assert f"{MAX_SOURCE_TEXTS}. obs {MAX_SOURCE_TEXTS - 1}" in content
    assert f"{MAX_SOURCE_TEXTS + 1}." not in content


def test_parse_accepts_dict_and_embedded_json():
    assert parse_synthesis_response(
        {"skip": False, "text": "Consistently prefers async written updates.", "reason": "r"}
    ) == "Consistently prefers async written updates."
    raw = 'Here you go:\n{"skip": false, "text": "Values blunt feedback in reviews.", "reason": "r"}'
    assert parse_synthesis_response(raw) == "Values blunt feedback in reviews."


def test_parse_rejects_skip_garbage_and_degenerate_lengths():
    assert parse_synthesis_response({"skip": True, "text": "", "reason": "diverges"}) is None
    assert parse_synthesis_response("no json here") is None
    assert parse_synthesis_response({"skip": False, "text": "short"}) is None
    assert parse_synthesis_response({"skip": False, "text": "x" * 600}) is None
    assert parse_synthesis_response(None) is None


# ------------------------------------------------------------------
# validator: consolidation_rules
# ------------------------------------------------------------------

def test_validator_accepts_good_rules():
    m = copy.deepcopy(MANIFEST)
    m["consolidation_rules"] = [
        {"from_types": ["takeaway"], "produce_type": "trait", "min_patches": 4,
         "guidance": "g"},
    ]
    ok, errors = validate_manifest(m, "t")
    assert ok, errors


def test_validator_rejects_bad_rules_with_precise_errors():
    m = copy.deepcopy(MANIFEST)
    m["consolidation_rules"] = [
        {"from_types": ["ghost"], "produce_type": "trait"},
        {"from_types": ["takeaway"], "produce_type": "ghost", "min_patches": 1,
         "surprise_key": True},
    ]
    ok, errors = validate_manifest(m, "t")
    assert not ok
    joined = "\n".join(errors)
    assert "undeclared patch types: ['ghost']" in joined
    assert "produce_type must be a declared patch type" in joined
    assert "min_patches must be an integer >= 2" in joined
    assert "surprise_key" in joined
