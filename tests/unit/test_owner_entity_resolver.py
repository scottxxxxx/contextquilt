"""owner_entity_id: the server resolution SS asked for (boundary piece 2).

The pure resolver, plus source guards for the quilt wiring. A wrong
link on a served item is worse than a null, so the resolver has NO
heuristic leg: exact name, recorded alias, or None.
"""

import re
from pathlib import Path

from contextquilt.services.people_identity import build_entity_resolver

SRC = Path(__file__).resolve().parents[2] / "src"
MAIN = (SRC / "main.py").read_text()


def resolver():
    entities = [
        {"entity_id": "e-canon", "name": "Vijay Rayudu", "merged_into": None},
        {"entity_id": "e-dead", "name": "Vijay (VJ)", "merged_into": "e-canon"},
        {"entity_id": "e-other", "name": "Pallavi", "merged_into": None},
    ]
    aliases = [
        {"alias": "VJ", "entity_id": "e-canon"},
        {"alias": "Vijay R", "entity_id": "e-dead"},  # alias on a folded row
    ]
    return build_entity_resolver(entities, aliases)


def test_exact_name_resolves_case_insensitively():
    r = resolver()
    assert r("vijay rayudu") == "e-canon"
    assert r("  Pallavi ") == "e-other"


def test_alias_resolves():
    assert resolver()("VJ") == "e-canon"


def test_merged_rows_resolve_forward():
    """A folded entity's name, and even an alias still pointing at the
    folded row, land on the canonical: the client must never receive an
    entity_id that 404s or forwards on them."""
    r = resolver()
    assert r("Vijay (VJ)") == "e-canon"
    assert r("Vijay R") == "e-canon"


def test_unknown_owner_is_null_not_a_guess():
    """No heuristic leg on purpose. "Sukumar / Joy" and "Speaker 2"
    resolve to nothing; null means CQ cannot tell."""
    r = resolver()
    assert r("Sukumar / Joy") is None
    assert r("Speaker 2") is None
    assert r(None) is None
    assert r("") is None


# --------------------------------------------------------------------
# Quilt wiring
# --------------------------------------------------------------------

def test_quilt_items_carry_the_field_and_completables_resolve():
    assert "owner_entity_id: Optional[str] = None" in MAIN
    m = re.search(r"owner_entity_id=\((.*?)\),\n\s+connections=", MAIN, re.DOTALL)
    assert m, "item construction does not compute owner_entity_id"
    body = m.group(1)
    # Edge person first, raw owner string second, nothing for non-items.
    assert body.index("owner_text_by_item") < body.index('value.get("owner")')
    assert "if row[\"patch_type\"] in completable else None" in body


def test_resolution_is_set_based_and_vocabulary_aware():
    """One edges query, one entities query, one aliases query per quilt
    call — never per item — and every one keyed on the caller's people
    vocabulary, not the SS literals."""
    assert "DISTINCT ON (pc.to_patch_id)" in MAIN
    assert "vocab.ownership_label," in MAIN
    assert MAIN.count("vocab.person_entity_type,\n    )") >= 1 or "user_id, vocab.person_entity_type," in MAIN


# --- a contested name resolves to nobody -------------------------------

def test_two_people_sharing_a_name_resolve_to_neither():
    """Measured on production 2026-08-17: Mike DiTroia and Mike Rogers
    both have meetings on one project, and three Pallavis share another.
    The resolver used to keep whichever row came first out of a query
    with no ORDER BY, so a contested name resolved by accident and the
    result read as certainty everywhere downstream."""
    resolve = build_entity_resolver(
        [{"entity_id": "e1", "name": "Mike", "merged_into": None},
         {"entity_id": "e2", "name": "Mike", "merged_into": None}],
        [],
    )
    assert resolve("Mike") is None


def test_an_alias_colliding_with_another_entitys_name_is_contested_too():
    """The collision does not have to be name-against-name to be real."""
    resolve = build_entity_resolver(
        [{"entity_id": "e1", "name": "Pallavi Vijay", "merged_into": None},
         {"entity_id": "e2", "name": "Pallavi Kandanur", "merged_into": None}],
        [{"entity_id": "e1", "alias": "Pallavi"},
         {"entity_id": "e2", "alias": "Pallavi"}],
    )
    assert resolve("Pallavi") is None
    # The unambiguous full names still work.
    assert resolve("Pallavi Vijay") == "e1"


def test_forms_that_merge_to_one_entity_are_not_contested():
    """An alias and its canonical name point at the same human, so they
    must keep resolving. Treating that as ambiguity would break every
    merged identity on the roster."""
    resolve = build_entity_resolver(
        [{"entity_id": "old", "name": "Sukumar", "merged_into": "new"},
         {"entity_id": "new", "name": "Sukumar Gurugubelli", "merged_into": None}],
        [{"entity_id": "old", "alias": "Sukumar"}],
    )
    assert resolve("Sukumar") == "new"
    assert resolve("Sukumar Gurugubelli") == "new"


def test_an_unknown_form_still_resolves_to_nothing():
    resolve = build_entity_resolver(
        [{"entity_id": "e1", "name": "Denby", "merged_into": None}], [])
    assert resolve("Nobody") is None
    assert resolve(None) is None
