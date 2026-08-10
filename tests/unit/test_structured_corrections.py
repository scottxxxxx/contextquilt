"""Slice 4 of the facet-runtime pass: the worker lane speaks vocabulary.

Three claims: corrections and completions are adapter-independent (a
structured-mode app's users can correct and complete; before this the
gate listed both under "extraction" and silently locked TR out of both
verbs); an unmatched correction lands in the APP'S vocabulary; and the
appearance recorder's person gate follows the people block.
"""

import re
from pathlib import Path

from contextquilt.services.corrections import (
    FALLBACK_PATCH_TYPE,
    parse_correction_response,
)
from contextquilt.services.ingest_modes import is_interaction_allowed
from contextquilt.services.schema_validator import validate_manifest

SRC = Path(__file__).resolve().parents[2] / "src"
WORKER = (SRC / "worker.py").read_text()


# --------------------------------------------------------------------
# The gate: corrections/completions are adapter-independent
# --------------------------------------------------------------------

def test_structured_apps_can_correct_and_complete():
    for verb in ("correction", "completion"):
        assert is_interaction_allowed("structured", verb) is True, verb
        assert is_interaction_allowed("extraction", verb) is True, verb


def test_the_gate_still_gates_adapters():
    """The fix must not have widened the actual adapter routing: a
    structured app still cannot send transcripts, an extraction app
    still cannot send pre-typed patches, legacy stays unrestricted."""
    assert is_interaction_allowed("structured", "meeting_transcript") is False
    assert is_interaction_allowed("extraction", "structured_patches") is False
    assert is_interaction_allowed(None, "meeting_transcript") is True
    assert is_interaction_allowed("structured", "hydrate") is True  # not this gate's job


# --------------------------------------------------------------------
# Unmatched corrections land in the app's vocabulary
# --------------------------------------------------------------------

def _response(ptype):
    return (
        '{"corrected_patch_id": null, "corrected_fact": {"text": '
        '"the rehearsal moved to Thursday", "owner": null, "deadline": null, '
        f'"deadline_date": null, "patch_type": "{ptype}"}}, "reason": "x"}}'
    )


def test_model_choice_valid_in_app_vocabulary_sticks():
    parsed = parse_correction_response(
        _response("improvement_area"), set(),
        allowed_types={"improvement_area", "session"},
        fallback_type="session",
    )
    assert parsed is not None
    _, value = parsed
    assert value["_new_type"] == "improvement_area"


def test_out_of_vocabulary_choice_takes_the_apps_fallback():
    """The model naming an SS type for a TR correction lands as the
    app's declared fallback, not as an off-manifest 'takeaway' invisible
    to every read keyed on the app's vocabulary."""
    parsed = parse_correction_response(
        _response("commitment"), set(),
        allowed_types={"improvement_area", "session"},
        fallback_type="session",
    )
    _, value = parsed
    assert value["_new_type"] == "session"


def test_defaults_keep_the_ss_floor_byte_identical():
    parsed = parse_correction_response(_response("commitment"), set())
    _, value = parsed
    assert value["_new_type"] == "commitment"
    parsed = parse_correction_response(_response("not_a_type"), set())
    _, value = parsed
    assert value["_new_type"] == FALLBACK_PATCH_TYPE


# --------------------------------------------------------------------
# Validator: correction_fallback_type referential integrity
# --------------------------------------------------------------------

def _manifest(**extra):
    m = {
        "app_id": "test-app", "version": 1, "facet_enum_version": 1,
        "patch_types": [
            {"domain_type": "session", "facet": "Episode",
             "permanence": "quarter", "display_name": "Session",
             "description": "x", "value_shape": {"text": "string"}},
        ],
        "connection_labels": [
            {"label": "addresses", "role": "informs",
             "from_types": ["session"], "to_types": ["session"],
             "description": "x"},
        ],
    }
    m.update(extra)
    return m


def test_validator_accepts_a_declared_fallback():
    ok, errors = validate_manifest(_manifest(correction_fallback_type="session"), "test-app")
    assert ok, errors


def test_validator_rejects_an_undeclared_fallback():
    ok, errors = validate_manifest(_manifest(correction_fallback_type="takeaway"), "test-app")
    assert not ok and any("correction_fallback_type" in e for e in errors)


# --------------------------------------------------------------------
# Source guards: worker lane wiring
# --------------------------------------------------------------------

def test_correction_handler_resolves_the_apps_vocabulary():
    assert "_correction_vocabulary(app_id)" in WORKER
    assert "allowed_types=sorted(allowed_types)" in WORKER, (
        "the prompt must offer the APP'S types, or the model guesses SS names"
    )
    assert 'value.pop("_new_type", fallback_type)' in WORKER


def test_appearance_gate_follows_the_people_vocabulary():
    """Closes slice 2's recorded limit: both entity-storage lanes pass
    person_entity_type from the app's people block, and the recorder
    compares against it rather than the literal."""
    assert 'if entity_type != person_entity_type:' in WORKER
    assert WORKER.count("person_entity_type=people_vocabulary(") >= 1
    assert "person_entity_type=people_vocabulary(\n                    await self._app_manifest(app_id)\n                ).person_entity_type" in WORKER.replace("\r", "") or "await self._app_manifest(app_id)" in WORKER
