"""A role states whose it is, or it is not stored.

Scott opened a project quilt on 2026-09-07 and read a tile that said
"Lead developer" with no name on it. The stored value was the whole
thing: {"text": "Lead developer", "headline": "Lead developer"}, no
owner, no held_by, and one edge, to the PROJECT. Measured across his
account: 62 of 77 active role patches had no person by any route.
"""

import pytest

from contextquilt.services.extraction_schema import enforce_role_holder


def _role(text, **value):
    return {"type": "role", "value": {"text": text, **value}}


def _person(name):
    return {"type": "person", "value": {"text": name}}


def test_a_role_with_no_holder_anywhere_is_dropped():
    """The exact prod row."""
    content = {"patches": [_role("Lead developer")]}
    enforce_role_holder(content)
    assert content["patches"] == []
    audit = content["_role_holder_enforced"]
    assert audit["dropped_count"] == 1
    assert audit["dropped"][0]["text"] == "Lead developer"


def test_an_existing_describes_edge_is_left_alone():
    role = _role("Lead developer")
    role["connects_to"] = [{"target_text": "Ashby", "target_type": "person",
                            "role": "informs", "label": "describes"}]
    content = {"patches": [role, _person("Ashby")]}
    enforce_role_holder(content)
    assert len(content["patches"]) == 2
    assert "_role_holder_enforced" not in content


def test_a_project_edge_is_not_a_holder():
    """THE DEFECT'S EXACT SHAPE. 61 of the 62 had an edge, to a project,
    and an earlier reading of mine nearly called that 'has a person'."""
    role = _role("Lead developer")
    role["connects_to"] = [{"target_text": "Twit", "target_type": "project",
                            "role": "parent", "label": "belongs_to"}]
    content = {"patches": [role]}
    enforce_role_holder(content)
    assert content["patches"] == []
    assert content["_role_holder_enforced"]["dropped_count"] == 1


def test_held_by_is_repaired_into_a_describes_edge():
    content = {"patches": [_role("Lead developer", held_by="Ashby")]}
    enforce_role_holder(content)
    assert len(content["patches"]) == 1
    edge = content["patches"][0]["connects_to"][0]
    assert edge == {"target_text": "Ashby", "target_type": "person",
                    "role": "informs", "label": "describes"}
    assert content["_role_holder_enforced"]["repaired"][0]["source"] == "held_by"


def test_a_name_prefix_in_the_text_is_repaired():
    """The shape the model DOES produce sometimes, e.g. 'Annapurna
    Patcharla leads HDBot development and owns definition of done'."""
    content = {"patches": [
        _role("Annapurna Patcharla leads HDBot development"),
        _person("Annapurna Patcharla"),
    ]}
    enforce_role_holder(content)
    assert len(content["patches"]) == 2
    role = content["patches"][0]
    assert role["connects_to"][0]["target_text"] == "Annapurna Patcharla"
    assert content["_role_holder_enforced"]["repaired"][0]["source"] == "text_prefix"


def test_the_longest_matching_name_wins():
    """A shorter name matching first would attach the role to the wrong
    person while looking like a successful repair."""
    content = {"patches": [
        _role("Anna Patcharla leads HDBot development"),
        _person("Anna"),
        _person("Anna Patcharla"),
    ]}
    enforce_role_holder(content)
    assert content["patches"][0]["connects_to"][0]["target_text"] == "Anna Patcharla"


def test_it_refuses_to_guess_the_only_person_in_the_meeting():
    """THE RULE THAT MATTERS MOST. One person present is not evidence the
    role is theirs, and a confidently wrong 'X is the lead developer' is
    worse than an absent tile."""
    content = {"patches": [_role("Lead developer"), _person("Ashby")]}
    enforce_role_holder(content)
    assert content["patches"] == [{"type": "person", "value": {"text": "Ashby"}}]
    assert content["_role_holder_enforced"]["dropped_count"] == 1
    assert content["_role_holder_enforced"]["repaired_count"] == 0


def test_the_submitting_user_is_not_a_holder():
    """`(you)`'s own role is not somebody else's attribution, and
    _is_real_person_owner already owns that judgement."""
    content = {"patches": [_role("Lead developer", owner="Scott")]}
    enforce_role_holder(content, user_label="Scott")
    assert content["patches"] == []


def test_a_placeholder_speaker_is_not_a_holder():
    content = {"patches": [_role("Lead developer", held_by="Speaker 3")]}
    enforce_role_holder(content)
    assert content["patches"] == []


def test_edges_pointing_at_a_dropped_role_are_stripped():
    """So Pass-2 cannot resynthesise the row as a stub, the same rule
    sanitize_behavior_observations follows."""
    other = {"type": "decision", "value": {"text": "ship it"},
             "connects_to": [
                 {"target_text": "Lead developer", "target_type": "role",
                  "role": "informs", "label": "motivated_by"},
                 {"target_text": "Twit", "target_type": "project",
                  "role": "parent", "label": "belongs_to"},
             ]}
    content = {"patches": [_role("Lead developer"), other]}
    enforce_role_holder(content)
    kept = content["patches"]
    assert len(kept) == 1
    assert [c["target_type"] for c in kept[0]["connects_to"]] == ["project"]


def test_other_patch_types_are_untouched():
    content = {"patches": [
        {"type": "decision", "value": {"text": "ship it"}},
        {"type": "commitment", "value": {"text": "send the deck"}},
    ]}
    enforce_role_holder(content)
    assert len(content["patches"]) == 2
    assert "_role_holder_enforced" not in content


def test_it_is_idempotent():
    content = {"patches": [_role("Lead developer", held_by="Ashby")]}
    enforce_role_holder(content)
    first = [dict(p) for p in content["patches"]]
    enforce_role_holder(content)
    assert content["patches"] == first
    assert len(content["patches"][0]["connects_to"]) == 1


@pytest.mark.parametrize("content", [
    {},
    {"patches": []},
    {"patches": [{"type": "role"}]},
    {"patches": [_role("")]},
])
def test_degenerate_input_never_raises(content):
    enforce_role_holder(content)
