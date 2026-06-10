"""Unit tests for is_placeholder_or_self_person.

The shared gate behind drop_placeholder_and_self_person_patches and
store_connected_patches Pass-2 stub synthesis. Pass-2 previously
bypassed the gate: a connects_to target naming the (you) speaker
re-created the self-person patch the sanitizer had just dropped.
"""

from src.contextquilt.services.extraction_schema import is_placeholder_or_self_person


def test_diarization_placeholders():
    assert is_placeholder_or_self_person("Speaker 3")
    assert is_placeholder_or_self_person("speaker_2")
    assert is_placeholder_or_self_person("Unknown")
    assert is_placeholder_or_self_person("Unidentified participant")


def test_self_reference_case_insensitive():
    assert is_placeholder_or_self_person("Ina", user_label="Ina")
    assert is_placeholder_or_self_person("ina", user_label="Ina")
    assert is_placeholder_or_self_person("  Ina  ", user_label="ina")


def test_real_people_pass():
    assert not is_placeholder_or_self_person("Scott", user_label="Ina")
    assert not is_placeholder_or_self_person("Maria")
    assert not is_placeholder_or_self_person("Sarah Liu", user_label="Sarah")  # prefix, not equality


def test_no_user_label_only_checks_placeholders():
    assert not is_placeholder_or_self_person("Ina", user_label=None)
    assert not is_placeholder_or_self_person("Ina", user_label="")
    assert is_placeholder_or_self_person("Speaker 1", user_label=None)


def test_non_string_and_empty_inputs():
    assert not is_placeholder_or_self_person(None)
    assert not is_placeholder_or_self_person("")
    assert not is_placeholder_or_self_person("   ")
    assert not is_placeholder_or_self_person(42, user_label="42")
