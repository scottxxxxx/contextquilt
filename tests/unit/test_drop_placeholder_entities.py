"""Unit tests for drop_placeholder_entities (Speaker-N leak guard)."""

from src.contextquilt.services.extraction_schema import drop_placeholder_entities


def _content(entity_names, relationships=None):
    return {
        "entities": [{"name": n, "type": "person", "description": "d"} for n in entity_names],
        "relationships": relationships or [],
    }


def test_drops_speaker_and_unknown_variants_keeps_real_names():
    c = _content(["Speaker 2", "speaker 10", "Speaker_4", "Unknown", "Unidentified male",
                  "Sarah Abrams", "Kore.ai"])
    drop_placeholder_entities(c)
    assert [e["name"] for e in c["entities"]] == ["Sarah Abrams", "Kore.ai"]
    audit = c["_placeholder_entities_enforced"]
    assert "speaker 2" in audit["entities_dropped"]
    assert "unknown" in audit["entities_dropped"]


def test_relationships_referencing_dropped_entities_are_removed():
    c = _content(
        ["Speaker 2", "Ryan"],
        relationships=[
            {"from": "Speaker 2", "to": "Ryan", "type": "works_with", "context": "x"},
            {"from": "Ryan", "to": "speaker 2", "type": "reports_to", "context": "x"},
            {"from": "Ryan", "to": "Cindy", "type": "works_with", "context": "x"},
        ],
    )
    drop_placeholder_entities(c)
    assert len(c["relationships"]) == 1
    assert c["relationships"][0]["to"] == "Cindy"
    assert c["_placeholder_entities_enforced"]["relationships_dropped"] == 2


def test_noop_when_nothing_to_drop():
    c = _content(["Sarah Abrams"], relationships=[{"from": "Sarah Abrams", "to": "Kore.ai", "type": "works_on"}])
    drop_placeholder_entities(c)
    assert len(c["entities"]) == 1
    assert len(c["relationships"]) == 1
    assert "_placeholder_entities_enforced" not in c


def test_real_names_starting_like_placeholders_are_kept():
    # The predicate is prefix-based on full placeholder words, not fuzzy —
    # a real surname must never be dropped.
    c = _content(["Spearman Kelly"])
    drop_placeholder_entities(c)
    assert len(c["entities"]) == 1


def test_tolerates_junk_shapes():
    c = {"entities": ["not a dict", {"type": "person"}, {"name": "Speaker 3", "type": "person"}],
         "relationships": "not a list"}
    drop_placeholder_entities(c)
    assert {"type": "person"} in c["entities"]
    assert not any(isinstance(e, dict) and e.get("name") == "Speaker 3" for e in c["entities"])
