"""The starter manifest templates must always pass the live validator —
a template that fails its own lint endpoint is worse than no template."""

import json
from pathlib import Path

import pytest

from src.contextquilt.services.schema_validator import validate_manifest

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates" / "manifests"
TEMPLATE_PATHS = sorted(TEMPLATES_DIR.glob("*.json"))


def _load(path):
    return json.loads(path.read_text())


def test_templates_directory_has_the_three_archetypes():
    names = {p.stem for p in TEMPLATE_PATHS}
    assert {"meeting-capture", "structured-coaching", "chat-assistant"} <= names


@pytest.mark.parametrize("path", TEMPLATE_PATHS, ids=lambda p: p.stem)
def test_template_passes_the_registration_validator(path):
    manifest = _load(path)
    ok, errors = validate_manifest(manifest, manifest["app_id"])
    assert ok, f"{path.stem}: {errors}"


@pytest.mark.parametrize("path", TEMPLATE_PATHS, ids=lambda p: p.stem)
def test_template_carries_placeholder_app_id_and_version_1(path):
    manifest = _load(path)
    assert manifest["app_id"] == "REPLACE-WITH-YOUR-APP-ID"
    assert manifest["version"] == 1
    assert manifest.get("ingest_mode") in ("extraction", "structured")


def test_structured_archetype_demonstrates_longitudinal():
    manifest = _load(TEMPLATES_DIR / "structured-coaching.json")
    longi = [pt for pt in manifest["patch_types"] if pt.get("longitudinal")]
    assert longi and all(pt.get("series_descriptor_field") for pt in longi)


def test_extraction_archetypes_demonstrate_cue_guidance():
    for name in ("meeting-capture", "chat-assistant"):
        manifest = _load(TEMPLATES_DIR / f"{name}.json")
        assert any(pt.get("cue_guidance") for pt in manifest["patch_types"]), name
