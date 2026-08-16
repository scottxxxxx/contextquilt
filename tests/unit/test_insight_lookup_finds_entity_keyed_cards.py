"""A card is reachable from the page it belongs to.

Three `what_stands_out` cards sat in production, correct in every
respect, and could not be served: the contrastive pass keys them on the
ENTITY (the identity that does not move when the extractor rephrases
somebody, which is the whole point of #250), while the read looked up
person PATCH ids. Two identities, one lookup, and the cards were
invisible to the only surface that renders them.

Measured 2026-08-16: source_person on all three held
49547cc9-... / c869be45-... / 989ff56d-..., which are entity ids, while
the query bound c7d18b24-... and 6f205581-..., which are Sukumar's
person patches.
"""

import pathlib

MAIN = pathlib.Path("src/main.py").read_text()


def test_the_insight_lookup_matches_the_entity_as_well_as_the_patches():
    assert "OR ($3::text IS NOT NULL" in MAIN
    assert "cp.value->>'source_entity_id' = $3" in MAIN


def test_the_entity_is_actually_bound_to_the_query():
    """The predicate is worthless if nobody passes the id."""
    assert 'str(row["entity_id"]) if row.get("entity_id") else None' in MAIN


def test_a_person_without_a_person_patch_still_gets_their_cards():
    """An entity-keyed card does not need a person patch to exist, so
    gating the fetch on one would hide it from exactly the thin people
    the lens can still speak about."""
    assert 'if row.get("patch_id") or row.get("entity_id"):' in MAIN


def test_the_patch_id_list_survives_a_person_with_no_patch():
    """The old expression indexed row["patch_id"] unconditionally, which
    would raise the moment the gate above let a patchless person through
    and the whole insights block would degrade to null."""
    assert '([row["patch_id"]] if row.get("patch_id") else [])' in MAIN
