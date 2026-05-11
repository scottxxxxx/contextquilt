"""Unit tests for scripts/split_compound_person_patches.py.

Covers the name-detection heuristic and the split logic. DB-touching
code is exercised in prod via dry-run; not unit-tested here.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "split_compound_person_patches.py"
_spec = importlib.util.spec_from_file_location("split_compound", _SCRIPT)
assert _spec and _spec.loader
mod = importlib.util.module_from_spec(_spec)
sys.modules["split_compound"] = mod
_spec.loader.exec_module(mod)


class TestIsCompoundPersonName:
    def test_simple_slash(self):
        assert mod.is_compound_person_name("Sai/Santosh")

    def test_slash_with_spaces(self):
        assert mod.is_compound_person_name("Chantelle / John")

    def test_three_names(self):
        assert mod.is_compound_person_name("Sai/Santosh/Ravi")

    def test_single_name_not_compound(self):
        assert not mod.is_compound_person_name("Sai")

    def test_sentence_not_compound(self):
        # "Joe is a participant in the meeting..." — full sentence in name field.
        assert not mod.is_compound_person_name(
            "Joe is a participant in the meeting and has commitments"
        )

    def test_prose_with_dash_not_compound(self):
        # "Sukumar - will coordinate with Jaffer..." — extraction-quality issue,
        # not a compound name. No slash, so it's safe regardless.
        assert not mod.is_compound_person_name(
            "Sukumar - will coordinate with Jaffer and provide client ID context"
        )

    def test_and_form_not_compound(self):
        # We deliberately don't split " and " — too ambiguous.
        assert not mod.is_compound_person_name("Benato and Whitley")

    def test_url_not_compound(self):
        assert not mod.is_compound_person_name("https://example.com")

    def test_lowercase_part_rejected(self):
        # Real names start uppercase. "sai/santosh" is suspicious.
        assert not mod.is_compound_person_name("sai/santosh")

    def test_empty(self):
        assert not mod.is_compound_person_name("")
        assert not mod.is_compound_person_name(None)

    def test_whitespace_only(self):
        assert not mod.is_compound_person_name("   ")

    def test_hyphenated_name_part_allowed(self):
        # "Jean-Luc" is a real first name — keep it as one part.
        assert mod.is_compound_person_name("Jean-Luc/Pierre")

    def test_apostrophe_name_part_allowed(self):
        assert mod.is_compound_person_name("O'Brien/Smith")


class TestSplitCompoundOwner:
    def test_splits_on_slash(self):
        assert mod.split_compound_owner("Sai/Santosh") == ["Sai", "Santosh"]

    def test_trims_whitespace(self):
        assert mod.split_compound_owner("Chantelle / John") == ["Chantelle", "John"]

    def test_three_parts(self):
        assert mod.split_compound_owner("A/B/C") == ["A", "B", "C"]

    def test_single_name(self):
        assert mod.split_compound_owner("Sai") == ["Sai"]

    def test_empty(self):
        assert mod.split_compound_owner("") == []
        assert mod.split_compound_owner(None) == []

    def test_does_not_split_on_and(self):
        # Conservative — " and " is ambiguous, leave it.
        assert mod.split_compound_owner("Benato and Whitley") == [
            "Benato and Whitley"
        ]
