"""The two pure decisions behind `you_owe`.

`is_self_owned` decides whose ledger an action item lands on, and
`manifest_declares_owed_to` decides whether CQ is allowed to answer the
question at all. Both are stated in the safe direction and these tests
exist to keep them there.
"""

import json
from pathlib import Path

import pytest

from contextquilt.services.people_identity import (
    capability_report,
    is_self_owned,
    manifest_declares_owed_to,
)


# --------------------------------------------------------------------
# is_self_owned
# --------------------------------------------------------------------

def test_null_owner_is_the_user():
    """What the extraction prompt actually asks for: the (you) speaker's
    own action items carry owner null, attribution is implicit."""
    assert is_self_owned(None, "Scott Guida") is True
    assert is_self_owned("", "Scott Guida") is True
    assert is_self_owned("   ", "Scott Guida") is True


@pytest.mark.parametrize("token", ["(you)", "you", "You", "self", "me", "I", "myself"])
def test_self_tokens_are_the_user(token):
    assert is_self_owned(token, "Scott Guida") is True


def test_full_display_name_is_the_user():
    assert is_self_owned("Scott Guida", "Scott Guida") is True
    assert is_self_owned("scott guida", "Scott Guida") is True


def test_first_token_of_the_display_name_is_the_user():
    """How the extractor writes it when it writes it at all. On prod today
    18 open completables say "Scott" and 3 say "Scott Guida" for a user
    whose display name is "Scott Guida"."""
    assert is_self_owned("Scott", "Scott Guida") is True


def test_a_named_third_party_is_not_the_user():
    assert is_self_owned("Lockridge Chen", "Scott Guida") is False
    assert is_self_owned("Guida", "Scott Guida") is False


def test_unrecognised_owner_is_not_the_user():
    """The predicate is an inclusion, not an exclusion. An owner CQ cannot
    resolve stays OUT of the user's ledger: absent understates, present
    overstates, and only one of those is a lie a memory product can
    afford."""
    assert is_self_owned("Speaker 2", "Scott Guida") is False
    assert is_self_owned("Unknown", "Scott Guida") is False


def test_missing_profile_falls_back_to_empty_and_self_tokens():
    """No display name is not a reason to claim every item."""
    assert is_self_owned(None, None) is True
    assert is_self_owned("(you)", None) is True
    assert is_self_owned("Scott", None) is False
    assert is_self_owned("Lockridge Chen", "") is False


def test_single_token_display_name_does_not_match_a_prefix():
    """"Scott" as a display name must not swallow "Scottie"."""
    assert is_self_owned("Scott", "Scott") is True
    assert is_self_owned("Scottie", "Scott") is False


def test_non_string_owner_is_not_the_user():
    assert is_self_owned(42, "Scott Guida") is False
    assert is_self_owned(["Scott"], "Scott Guida") is False


# --------------------------------------------------------------------
# manifest_declares_owed_to
# --------------------------------------------------------------------

def test_manifest_with_the_label_declares_it():
    manifest = {"connection_labels": [{"label": "owns"}, {"label": "owed_to"}]}
    assert manifest_declares_owed_to(manifest) is True


def test_manifest_without_the_label_does_not():
    manifest = {"connection_labels": [{"label": "owns"}, {"label": "works_on"}]}
    assert manifest_declares_owed_to(manifest) is False


def test_junk_manifests_degrade_to_false():
    """False is the conservative direction: the caller gets null and a
    stated reason rather than an empty list that reads as "nothing"."""
    for junk in (None, {}, "", [], {"connection_labels": None},
                 {"connection_labels": "owed_to"}, {"connection_labels": [None, "x"]}):
        assert manifest_declares_owed_to(junk) is False


def test_the_shipped_ss_manifest_declares_it():
    """The registered manifest is what actually turns the capability on in
    production, so a version bump that drops the label would silently take
    the ledger back to null. Pin it."""
    path = Path(__file__).resolve().parents[2] / "init-db" / "11_shouldersurf_schema.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["version"] >= 9
    assert manifest_declares_owed_to(manifest) is True
    spec = next(c for c in manifest["connection_labels"] if c["label"] == "owed_to")
    # Direction is the whole point: item to person, the mirror of `owns`.
    assert spec["from_types"] == ["commitment", "blocker"]
    assert spec["to_types"] == ["person"]


# --------------------------------------------------------------------
# capability_report
# --------------------------------------------------------------------

def test_capability_defaults_to_unavailable_with_a_reason():
    caps = capability_report()
    assert caps["you_owe"]["available"] is False
    assert "owed_to" in caps["you_owe"]["reason"]


def test_capability_flips_on_for_a_declaring_app():
    caps = capability_report(owed_to_available=True)
    assert caps["you_owe"] == {"available": True, "reason": None}


def test_capability_flip_touches_nothing_else():
    """Two apps reading the same user can honestly get different answers
    for you_owe, but nothing else in the block is per-app."""
    off = capability_report(False)
    on = capability_report(True)
    assert {k: v for k, v in off.items() if k != "you_owe"} == \
           {k: v for k, v in on.items() if k != "you_owe"}
    assert on["confirmed_mention_split"]["available"] is False


def test_report_is_a_copy_not_the_module_constant():
    caps = capability_report(True)
    caps["you_owe"]["available"] = "mutated"
    assert capability_report(True)["you_owe"]["available"] is True


def test_is_self_owned_and_the_sanitizer_agree_on_every_form():
    """These two ran on different rules once and the gap was a live bug:
    the write path allowed owed_to "Scott" on an item owned by "Scott
    Guida", and the read path then counted it as the user's own. They
    share `is_user_reference` now, and this asserts they cannot drift
    apart again."""
    from contextquilt.services.extraction_schema import is_user_reference
    for form in ("Scott", "Scott Guida", "scott", "(you)", "you", "me", "myself"):
        assert is_self_owned(form, "Scott Guida") is True, form
        assert is_user_reference(form, "Scott Guida") is True, form
    for form in ("Lockridge Chen", "Guida", "Speaker 2"):
        assert is_self_owned(form, "Scott Guida") is False, form
        assert is_user_reference(form, "Scott Guida") is False, form
