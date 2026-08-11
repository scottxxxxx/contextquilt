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
