"""The People vocabulary: manifest-declared roles, SS floor for legacy.

Slice 2 of the facet-runtime pass. The People surface spoke SS's dialect
(person / person / owns / works_on / owed_to) as literals; the optional
manifest `people` block names those roles per app, and these tests pin
the resolution rules, the validator's referential integrity, and the
byte-identity floor for manifests registered before the block existed.
"""

import re
from pathlib import Path

from contextquilt.services.people_identity import (
    DEFAULT_PEOPLE_VOCABULARY,
    manifest_declares_owed_to,
    people_vocabulary,
)
from contextquilt.services.schema_validator import validate_manifest

SRC = Path(__file__).resolve().parents[2] / "src"
MAIN = (SRC / "main.py").read_text()


# --------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------

def test_legacy_manifest_gets_the_ss_floor():
    """Every manifest registered before the block existed resolves to
    the SS-default vocabulary, so ShoulderSurf and GhostPour behave
    byte-identically without a re-registration."""
    for m in (None, {}, {"patch_types": []}, {"people": None}, {"people": {}}):
        assert people_vocabulary(m) == DEFAULT_PEOPLE_VOCABULARY


def test_explicit_block_is_taken_at_its_word():
    v = people_vocabulary({"people": {
        "person_type": "contact",
        "person_entity_type": "contact",
        "ownership_label": "assigned_to",
        "works_on_label": "engaged_on",
        "counterparty_label": "awaits_from",
    }})
    assert (v.person_type, v.person_entity_type) == ("contact", "contact")
    assert (v.ownership_label, v.works_on_label) == ("assigned_to", "engaged_on")
    assert v.counterparty_label == "awaits_from"


def test_entity_type_defaults_to_the_patch_type_name():
    """Most apps use one word for both; requiring the repetition would
    invite drift."""
    v = people_vocabulary({"people": {"person_type": "contact"}})
    assert v.person_entity_type == "contact"


def test_explicit_block_without_counterparty_means_not_tracked():
    """The one field that does NOT default: an app that declares a
    people block and omits counterparty_label is saying "you owe them"
    is not tracked, and you_owe must stay null even if a label named
    owed_to happens to exist in its vocabulary."""
    m = {
        "people": {"person_type": "contact", "ownership_label": "assigned_to"},
        "connection_labels": [{"label": "owed_to"}],
    }
    assert people_vocabulary(m).counterparty_label is None
    assert manifest_declares_owed_to(m) is False


def test_owed_to_availability_follows_the_apps_own_label():
    ss_like = {"connection_labels": [{"label": "owed_to"}]}
    assert manifest_declares_owed_to(ss_like) is True  # legacy floor, unchanged

    custom = {
        "people": {"person_type": "contact", "counterparty_label": "awaits_from"},
        "connection_labels": [{"label": "awaits_from"}],
    }
    assert manifest_declares_owed_to(custom) is True

    undeclared = {
        "people": {"person_type": "contact", "counterparty_label": "awaits_from"},
        "connection_labels": [{"label": "assigned_to"}],
    }
    assert manifest_declares_owed_to(undeclared) is False


# --------------------------------------------------------------------
# Validator: referential integrity for the block
# --------------------------------------------------------------------

def _manifest_with(people_block):
    return {
        "app_id": "test-app", "version": 1, "facet_enum_version": 1,
        "patch_types": [
            {"domain_type": "contact", "facet": "Connection",
             "permanence": "decade", "display_name": "Contact",
             "description": "x", "value_shape": {"text": "string"}},
            {"domain_type": "task", "facet": "Episode", "completable": True,
             "permanence": "month", "display_name": "Task",
             "description": "x", "value_shape": {"text": "string"}},
        ],
        "connection_labels": [
            {"label": "assigned_to", "role": "informs",
             "from_types": ["contact"], "to_types": ["task"],
             "description": "x"},
        ],
        "entity_types": [
            {"entity_type": "contact", "display_name": "Contact", "description": "x"},
        ],
        "people": people_block,
    }


def test_validator_accepts_a_coherent_block():
    ok, errors = validate_manifest(_manifest_with({
        "person_type": "contact",
        "ownership_label": "assigned_to",
    }), "test-app")
    assert ok, errors


def test_validator_requires_declared_referents():
    """A people block pointing at undeclared types would turn the People
    surface on with a vocabulary the extraction can never produce, the
    capability-that-lies failure. Every role must resolve."""
    ok, errors = validate_manifest(_manifest_with({
        "person_type": "ghost",
        "person_entity_type": "phantom",
        "ownership_label": "no_such_label",
    }), "test-app")
    assert not ok
    text = " ".join(errors)
    assert "person_type" in text and "ghost" in text
    assert "person_entity_type" in text and "phantom" in text
    assert "ownership_label" in text and "no_such_label" in text


def test_validator_rejects_unknown_and_missing_keys():
    ok, errors = validate_manifest(_manifest_with({
        "person_type": "contact", "person_flavor": "vanilla",
    }), "test-app")
    assert not ok and any("person_flavor" in e for e in errors)

    ok, errors = validate_manifest(_manifest_with({"ownership_label": "assigned_to"}), "test-app")
    assert not ok and any("person_type" in e for e in errors)


def test_manifest_without_the_block_still_validates():
    m = _manifest_with(None)
    del m["people"]
    ok, errors = validate_manifest(m, "test-app")
    assert ok, errors


# --------------------------------------------------------------------
# Source guards: the dialect literals stay replaced
# --------------------------------------------------------------------

def test_people_surface_speaks_no_literals():
    """No bare 'person' / 'owns' / 'works_on' / 'owed_to' left in the
    People surface's SQL; every one resolves through the caller's
    vocabulary. A new query written with a literal fails here."""
    for probe in (
        "entity_type = 'person'",
        "patch_type = 'person'",
        "connection_label = 'owns'",
        "connection_label = 'works_on'",
        "connection_label = 'owed_to'",
    ):
        assert probe not in MAIN, f"People literal survived: {probe}"


def test_both_read_routes_resolve_the_context_once():
    assert MAIN.count("_people_read_context(conn, app_id)") >= 4, (
        "list, detail, and the identity writes each resolve the caller's vocabulary"
    )
    assert "_people_owed_to_available" not in MAIN, "the old single-purpose helper is retired"
