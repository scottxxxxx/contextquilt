"""Telling "Pradeep & Suresh" from "Steven".

SS built an unplaced-owner surface that renders "Which Steven?" on each
row. On Scott's real data three of four rows would read "Which Pradeep &
Suresh?", which is not a question anybody can answer. The two kinds need
different affordances, and only one of them is resolvable at all.

What CQ knows that a client does not is the ROSTER. It does NOT know
this at extraction time: the model writes `value.owner` as free text and
never says how many humans are in it, so a server-side punctuation guess
would be the same guess further from the user.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from contextquilt.services.people_identity import (
    owner_names_multiple,
    split_owner_string,
)

MAIN = (ROOT / "src" / "main.py").read_text()

LIVE = {"pradeep", "suresh", "vijay", "sukumar", "steven", "joy"}


def resolve(name):
    return "entity" if (name or "").strip().lower() in LIVE else None


def test_two_known_people_is_confirmed_not_inferred():
    """True requires the ROSTER to confirm both halves, which is the
    whole reason this lives on the server."""
    assert owner_names_multiple("Pradeep & Suresh", resolve) is True
    assert owner_names_multiple("Vijay and Sukumar", resolve) is True


def test_a_single_name_is_a_confident_no():
    assert owner_names_multiple("Steven", resolve) is False
    assert owner_names_multiple("Speaker 1", resolve) is False


def test_compound_but_unconfirmable_is_null_not_a_guess():
    """"Pradeep & the vendor" has a separator and one known person. It
    is probably two parties and CQ cannot prove it, so it says so. That
    null is exactly where a client-side heuristic belongs."""
    assert owner_names_multiple("Pradeep & the vendor", resolve) is None
    assert owner_names_multiple("Smith, John", resolve) is None


def test_a_slash_is_not_a_conjunction():
    """"QA/dev" is one team, not two colleagues. Splitting on it would
    manufacture compounds out of ordinary shorthand."""
    assert split_owner_string("QA/dev team") == ["QA/dev team"]
    assert owner_names_multiple("QA/dev team", resolve) is False


def test_junk_never_raises_on_a_read_route():
    for junk in (None, "", "   ", 42, ["a"]):
        assert owner_names_multiple(junk, resolve) is False


def test_a_missing_resolver_yields_null_not_false():
    """No roster means cannot tell, never a confident single. False here
    would tell a client "one person" about a string it never checked."""
    assert owner_names_multiple("Pradeep & Suresh", None) is None


def test_the_field_is_declared_and_populated():
    """Declared but never assigned ships null forever and reads as
    'no compound owners exist', which is the silent half of this bug
    class."""
    assert "owner_names_multiple: Optional[bool] = None" in MAIN
    assert "owner_names_multiple=(" in MAIN
    assert "owner_names_multiple(value.get(\"owner\"), resolve_owner_entity)" in MAIN


def test_it_is_scoped_to_completables():
    """A trait or a preference has no owner to be shared, and answering
    False there would be a claim about a question that does not apply."""
    block = MAIN.split("owner_names_multiple=(")[1][:220]
    assert "in completable else None" in block
