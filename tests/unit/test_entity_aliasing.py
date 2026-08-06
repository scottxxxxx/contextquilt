"""Unit tests for the entity alias heuristics.

These rules decide when two entity names are surface forms of the same
real-world thing. The bar is deliberately conservative: merges happen
only on a unique candidate, so a false positive requires both a rule
match AND no competing entity of the same type.
"""

from src.contextquilt.services.entity_aliasing import (
    find_alias_candidate,
    is_alias_form,
    tokenize_name,
)


# ============================================================
# tokenize_name
# ============================================================


def test_tokenize_basic_and_punctuation():
    assert tokenize_name("Lockridge Abrams") == ["lockridge", "abrams"]
    assert tokenize_name("L. Abrams") == ["l", "abrams"]
    assert tokenize_name("Axiom Industries") == ["axiom", "industries"]


def test_tokenize_unicode_names_hold_together():
    assert tokenize_name("José García") == ["josé", "garcía"]


def test_tokenize_garbage():
    assert tokenize_name("") == []
    assert tokenize_name(None) == []
    assert tokenize_name("...") == []


# ============================================================
# is_alias_form
# ============================================================


def test_first_name_is_alias_of_full_name():
    assert is_alias_form("Lockridge", "Lockridge Abrams")


def test_last_name_is_alias_of_full_name():
    assert is_alias_form("Abrams", "Lockridge Abrams")


def test_initial_expansion():
    assert is_alias_form("L. Abrams", "Lockridge Abrams")
    assert is_alias_form("L Abrams", "Lockridge Abrams")


def test_company_short_form():
    assert is_alias_form("Axiom", "Axiom Industries")


def test_not_alias_of_itself():
    assert not is_alias_form("Lockridge Abrams", "Lockridge Abrams")
    assert not is_alias_form("lockridge abrams", "Lockridge Abrams")


def test_longer_is_not_alias_of_shorter():
    assert not is_alias_form("Lockridge Abrams", "Lockridge")


def test_different_people_not_aliases():
    assert not is_alias_form("Lockridge Chen", "Lockridge Abrams")
    assert not is_alias_form("Maria", "Lockridge Abrams")


def test_reordered_tokens_not_aliases():
    assert not is_alias_form("Abrams Lockridge", "Lockridge Abrams")


def test_multiword_substring_not_enough():
    # Multi-letter tokens never expand, "Lock" is not "Lockridge" here
    assert not is_alias_form("Loc Abrams", "Lockridge Abrams")


def test_initial_only_claims_leftover_tokens():
    # "M. Lockridge" against "Lockridge Mayfield": "lockridge" consumes the exact
    # token, so the initial must expand against "mayfield"
    assert is_alias_form("M. Lockridge", "Lockridge Mayfield")
    # ...but "M. Lockridge" against plain "Lockridge" has nothing left for M
    assert not is_alias_form("M. Lockridge", "Lockridge")


# ============================================================
# find_alias_candidate
# ============================================================


def test_unique_short_form_match():
    existing = [(1, "Lockridge Abrams"), (2, "Jordan Melville")]
    assert find_alias_candidate("Lockridge", existing) == (1, "Lockridge Abrams", "name_is_alias")


def test_unique_canonical_direction():
    # The fuller form arrives after the short one was stored
    existing = [(1, "Lockridge"), (2, "Jordan Melville")]
    assert find_alias_candidate("Lockridge Abrams", existing) == (1, "Lockridge", "name_is_canonical")


def test_ambiguity_blocks_merge():
    existing = [(1, "Lockridge Abrams"), (2, "Lockridge Chen")]
    assert find_alias_candidate("Lockridge", existing) is None


def test_no_match():
    existing = [(1, "Jordan Melville"), (2, "Axiom Industries")]
    assert find_alias_candidate("Lockridge", existing) is None


def test_initial_form_resolves():
    existing = [(1, "Lockridge Abrams"), (2, "Sam Abreu")]
    # "L. Abrams" expands against both? Sam Abreu: tokens [sam, abreu];
    # "abrams" is not a token of it, so only Lockridge Abrams matches.
    assert find_alias_candidate("L. Abrams", existing) == (1, "Lockridge Abrams", "name_is_alias")


def test_empty_existing():
    assert find_alias_candidate("Lockridge", []) is None


# ============================================================
# False-positive classes caught in the 2026-06-10 prod dry-run
# ============================================================


def test_diarization_placeholders_never_alias():
    # 'Speaker 1' -> 'Speaker 10' was proposed before this gate
    assert not is_alias_form("Speaker 1", "Speaker 10")
    assert find_alias_candidate("Speaker 1", [(1, "Speaker 10")]) is None


def test_you_marker_leakage_never_aliases():
    # 'Scott' -> 'Scott (you)': the marked row is the wrong one,
    # merging into it would entrench sanitizer leakage
    assert not is_alias_form("Scott", "Scott (you)")
    assert find_alias_candidate("Scott", [(1, "Scott (you)")]) is None


def test_possessive_is_not_identity():
    # 'Underhill' is a different person from "Underhill's Flatmate"
    assert not is_alias_form("Underhill", "Underhill's Flatmate (Swedish)")
    assert not is_alias_form("Underhill", "Underhill’s Flatmate")  # curly apostrophe


def test_digits_never_initial_expand():
    # Deliberate trade-off: also rejects benign 'June 9 meeting' ->
    # 'June 9th meeting', because digit expansion is what produced the
    # Speaker 1 -> Speaker 10 disaster class.
    assert not is_alias_form("June 9 meeting", "June 9th meeting")


def test_single_token_must_be_edge_token():
    # 'Artemis' buried mid-name is topical overlap, not an alias
    assert not is_alias_form("Artemis", "Ashcombe Agent Platform Artemis Edition")
    # ...while first/last token positions still work
    assert is_alias_form("Sam", "Sam Altman")
    assert is_alias_form("Quinby", "Su Quinby")
