"""Tests for the user_attribution_hint validator."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from contextquilt.services.attribution import (  # noqa: E402
    CONFIDENCE_BASIS_VALUES,
    validate_user_attribution_hint,
)


def _valid_hint(**overrides):
    base = {
        "speaker_label": "Speaker 3",
        "confidence": 0.42,
        "confidence_basis": "combined",
    }
    base.update(overrides)
    return base


def test_returns_none_when_absent():
    assert validate_user_attribution_hint(None) is None


def test_returns_none_when_not_a_dict():
    assert validate_user_attribution_hint("Speaker 3") is None
    assert validate_user_attribution_hint(["Speaker 3", 0.42]) is None
    assert validate_user_attribution_hint(0.42) is None


def test_minimum_valid_hint_passes():
    out = validate_user_attribution_hint(_valid_hint())
    assert out is not None
    assert out["speaker_label"] == "Speaker 3"
    assert out["confidence"] == 0.42
    assert out["confidence_basis"] == "combined"
    assert "secondary_candidate" not in out


def test_all_four_confidence_bases_accepted():
    for basis in CONFIDENCE_BASIS_VALUES:
        out = validate_user_attribution_hint(_valid_hint(confidence_basis=basis))
        assert out is not None, basis
        assert out["confidence_basis"] == basis


def test_unknown_confidence_basis_rejected():
    assert validate_user_attribution_hint(_valid_hint(confidence_basis="raw_cosine")) is None
    assert validate_user_attribution_hint(_valid_hint(confidence_basis="")) is None
    assert validate_user_attribution_hint(_valid_hint(confidence_basis=None)) is None


def test_confidence_must_be_numeric_and_in_range():
    assert validate_user_attribution_hint(_valid_hint(confidence="0.5")) is None
    assert validate_user_attribution_hint(_valid_hint(confidence=-0.1)) is None
    assert validate_user_attribution_hint(_valid_hint(confidence=1.01)) is None
    assert validate_user_attribution_hint(_valid_hint(confidence=True)) is None  # bools rejected
    assert validate_user_attribution_hint(_valid_hint(confidence=0.0)) is not None
    assert validate_user_attribution_hint(_valid_hint(confidence=1.0)) is not None


def test_speaker_label_must_be_non_empty_string():
    assert validate_user_attribution_hint(_valid_hint(speaker_label="")) is None
    assert validate_user_attribution_hint(_valid_hint(speaker_label="   ")) is None
    assert validate_user_attribution_hint(_valid_hint(speaker_label=None)) is None
    assert validate_user_attribution_hint(_valid_hint(speaker_label=3)) is None


def test_speaker_label_is_stripped():
    out = validate_user_attribution_hint(_valid_hint(speaker_label="  Speaker 3  "))
    assert out is not None
    assert out["speaker_label"] == "Speaker 3"


def test_secondary_candidate_optional():
    out = validate_user_attribution_hint(_valid_hint())
    assert out is not None
    assert "secondary_candidate" not in out


def test_secondary_candidate_valid_passes():
    hint = _valid_hint()
    hint["secondary_candidate"] = {"speaker_label": "Speaker 5", "confidence": 0.31}
    out = validate_user_attribution_hint(hint)
    assert out is not None
    assert out["secondary_candidate"] == {
        "speaker_label": "Speaker 5",
        "confidence": 0.31,
    }


def test_secondary_confidence_must_not_exceed_primary():
    hint = _valid_hint(confidence=0.3)
    hint["secondary_candidate"] = {"speaker_label": "Speaker 5", "confidence": 0.5}
    assert validate_user_attribution_hint(hint) is None


def test_secondary_confidence_equal_to_primary_allowed():
    hint = _valid_hint(confidence=0.5)
    hint["secondary_candidate"] = {"speaker_label": "Speaker 5", "confidence": 0.5}
    out = validate_user_attribution_hint(hint)
    assert out is not None


def test_malformed_secondary_rejects_whole_hint():
    hint = _valid_hint()
    hint["secondary_candidate"] = {"speaker_label": "Speaker 5"}  # missing confidence
    assert validate_user_attribution_hint(hint) is None

    hint = _valid_hint()
    hint["secondary_candidate"] = "Speaker 5"  # not a dict
    assert validate_user_attribution_hint(hint) is None


def test_int_confidence_cast_to_float():
    out = validate_user_attribution_hint(_valid_hint(confidence=1))
    assert out is not None
    assert isinstance(out["confidence"], float)
    assert out["confidence"] == 1.0
