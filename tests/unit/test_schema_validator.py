"""Unit tests for the app schema manifest validator."""

import copy

import pytest

from src.contextquilt.services.schema_validator import validate_manifest


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def minimal_valid_manifest():
    """The smallest manifest that should pass validation."""
    return {
        "app_id": "test-app",
        "version": 1,
        "facet_enum_version": 1,
        "patch_types": [
            {
                "domain_type": "note",
                "facet": "Episode",
                "permanence": "week",
                "display_name": "Note",
                "description": "A freeform note.",
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
def full_valid_manifest(minimal_valid_manifest):
    """A richer valid manifest with optional fields populated."""
    m = copy.deepcopy(minimal_valid_manifest)
    m.update(
        {
            "display_name": "Test App",
            "description": "A test app registering a minimal schema.",
            "origin_types": ["meeting"],
            "entity_types": [
                {
                    "entity_type": "person",
                    "display_name": "Person",
                    "description": "A named individual.",
                    "indexed": True,
                }
            ],
        }
    )
    return m


# ============================================================
# Happy path
# ============================================================


def test_minimal_manifest_validates(minimal_valid_manifest):
    ok, errors = validate_manifest(minimal_valid_manifest, "test-app")
    assert ok, f"Expected valid manifest to pass, got errors: {errors}"
    assert errors == []


def test_full_manifest_validates(full_valid_manifest):
    ok, errors = validate_manifest(full_valid_manifest, "test-app")
    assert ok, f"Expected full manifest to pass, got errors: {errors}"


# ============================================================
# Top-level errors
# ============================================================


def test_non_object_manifest_fails():
    ok, errors = validate_manifest("not an object", "test-app")
    assert not ok
    assert any("must be a JSON object" in e for e in errors)


def test_missing_required_keys(minimal_valid_manifest):
    del minimal_valid_manifest["patch_types"]
    ok, errors = validate_manifest(minimal_valid_manifest, "test-app")
    assert not ok
    assert any("patch_types" in e for e in errors)


def test_app_id_mismatch(minimal_valid_manifest):
    ok, errors = validate_manifest(minimal_valid_manifest, "different-app")
    assert not ok
    assert any("app_id" in e for e in errors)


def test_unknown_top_level_key(minimal_valid_manifest):
    minimal_valid_manifest["bogus_field"] = "oops"
    ok, errors = validate_manifest(minimal_valid_manifest, "test-app")
    assert not ok
    assert any("bogus_field" in e for e in errors)


def test_version_must_be_positive_int(minimal_valid_manifest):
    minimal_valid_manifest["version"] = 0
    ok, errors = validate_manifest(minimal_valid_manifest, "test-app")
    assert not ok
    assert any("version" in e for e in errors)


def test_unsupported_facet_enum_version(minimal_valid_manifest):
    minimal_valid_manifest["facet_enum_version"] = 99
    ok, errors = validate_manifest(minimal_valid_manifest, "test-app")
    assert not ok
    assert any("facet_enum_version" in e for e in errors)


# ============================================================
# Patch type errors
# ============================================================


def test_invalid_facet(minimal_valid_manifest):
    minimal_valid_manifest["patch_types"][0]["facet"] = "Whimsy"
    ok, errors = validate_manifest(minimal_valid_manifest, "test-app")
    assert not ok
    assert any("facet" in e and "Whimsy" in e for e in errors)


def test_invalid_permanence(minimal_valid_manifest):
    minimal_valid_manifest["patch_types"][0]["permanence"] = "forever"
    ok, errors = validate_manifest(minimal_valid_manifest, "test-app")
    assert not ok
    assert any("permanence" in e for e in errors)


def test_duplicate_patch_type_keys(minimal_valid_manifest):
    minimal_valid_manifest["patch_types"].append(
        copy.deepcopy(minimal_valid_manifest["patch_types"][0])
    )
    ok, errors = validate_manifest(minimal_valid_manifest, "test-app")
    assert not ok
    assert any("Duplicate patch_type.domain_type" in e for e in errors)


def test_empty_patch_types(minimal_valid_manifest):
    minimal_valid_manifest["patch_types"] = []
    ok, errors = validate_manifest(minimal_valid_manifest, "test-app")
    assert not ok
    assert any("patch_types" in e and "non-empty" in e for e in errors)


# ============================================================
# Connection label errors
# ============================================================


def test_invalid_connection_role(minimal_valid_manifest):
    minimal_valid_manifest["connection_labels"][0]["role"] = "controls"
    ok, errors = validate_manifest(minimal_valid_manifest, "test-app")
    assert not ok
    assert any("role" in e for e in errors)


def test_label_references_undeclared_type(minimal_valid_manifest):
    minimal_valid_manifest["connection_labels"][0]["to_types"] = ["does-not-exist"]
    ok, errors = validate_manifest(minimal_valid_manifest, "test-app")
    assert not ok
    assert any("undeclared" in e for e in errors)


def test_duplicate_connection_labels(minimal_valid_manifest):
    minimal_valid_manifest["connection_labels"].append(
        copy.deepcopy(minimal_valid_manifest["connection_labels"][0])
    )
    ok, errors = validate_manifest(minimal_valid_manifest, "test-app")
    assert not ok
    assert any("Duplicate connection_label" in e for e in errors)


# ============================================================
# Entity type errors
# ============================================================


def test_entity_types_accepts_valid_shape(full_valid_manifest):
    ok, errors = validate_manifest(full_valid_manifest, "test-app")
    assert ok, errors


def test_entity_types_must_be_array(full_valid_manifest):
    full_valid_manifest["entity_types"] = {"not": "an array"}
    ok, errors = validate_manifest(full_valid_manifest, "test-app")
    assert not ok
    assert any("entity_types" in e for e in errors)


def test_entity_type_missing_required(full_valid_manifest):
    del full_valid_manifest["entity_types"][0]["display_name"]
    ok, errors = validate_manifest(full_valid_manifest, "test-app")
    assert not ok
    assert any("display_name" in e for e in errors)


def test_duplicate_entity_types(full_valid_manifest):
    full_valid_manifest["entity_types"].append(
        copy.deepcopy(full_valid_manifest["entity_types"][0])
    )
    ok, errors = validate_manifest(full_valid_manifest, "test-app")
    assert not ok
    assert any("Duplicate entity_types" in e for e in errors)


def test_entity_indexed_must_be_bool(full_valid_manifest):
    full_valid_manifest["entity_types"][0]["indexed"] = "yes"
    ok, errors = validate_manifest(full_valid_manifest, "test-app")
    assert not ok
    assert any("indexed" in e for e in errors)


# ============================================================
# Origin types
# ============================================================


def test_origin_types_valid(full_valid_manifest):
    full_valid_manifest["origin_types"] = ["meeting", "typed_note"]
    ok, errors = validate_manifest(full_valid_manifest, "test-app")
    assert ok, errors


def test_origin_types_must_be_strings(full_valid_manifest):
    full_valid_manifest["origin_types"] = ["meeting", 42]
    ok, errors = validate_manifest(full_valid_manifest, "test-app")
    assert not ok
    assert any("origin_types" in e for e in errors)


# ============================================================
# Extraction prompt override
# ============================================================


def test_extraction_prompt_override_accepted(minimal_valid_manifest):
    minimal_valid_manifest["extraction_prompt_override"] = "You are a helpful extractor."
    ok, errors = validate_manifest(minimal_valid_manifest, "test-app")
    assert ok, errors


def test_extraction_prompt_override_must_be_string(minimal_valid_manifest):
    minimal_valid_manifest["extraction_prompt_override"] = {"not": "a string"}
    ok, errors = validate_manifest(minimal_valid_manifest, "test-app")
    assert not ok
    assert any("extraction_prompt_override" in e for e in errors)


# ============================================================
# Real-world smoke — validate the SS manifest fixture
# ============================================================


def test_shouldersurf_manifest_validates():
    """Make sure the SS manifest fixture we ship with can actually be registered."""
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

    ok, errors = validate_manifest(manifest, manifest["app_id"])
    assert ok, f"ShoulderSurf manifest failed validation: {errors}"
