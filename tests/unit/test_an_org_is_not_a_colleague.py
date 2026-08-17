"""A company does not appear under NEW FACES.

Scott, 2026-08-17, on his People list: "someone is showing up in the
people section, but it's not a person it's CIGNA, which is a customer
that Mike and I were talking about last week."

CIGNA rendered as a colleague with "Joined 1 of your rooms this week"
and "Met 6 days ago, 1 meeting, Kore".

The diagnosis is the interesting part. The entity row was typed
correctly as `org`, and every writer of person_appearances DOES gate on
entity type. What slipped through is that the same extraction response
emitted CIGNA BOTH as an `org` entity AND as a `person` patch, and the
person patch is what minted the person-typed entity behind the row.

So the contradiction was visible inside the payload the whole time and
needed no database lookup to catch. A name cannot be a company and a
colleague in the same meeting.
"""

from contextquilt.services.extraction_schema import (
    drop_placeholder_and_self_person_patches,
)


def _content(patches, entities=None):
    return {"patches": patches, "entities": entities or []}


def test_a_name_the_same_response_called_an_org_is_not_a_person():
    content = _content(
        [{"type": "person", "value": {"text": "CIGNA"}},
         {"type": "person", "value": {"text": "Mike DiTroia"}}],
        [{"type": "org", "name": "CIGNA"}],
    )
    kept = drop_placeholder_and_self_person_patches(content)["patches"]
    assert [p["value"]["text"] for p in kept] == ["Mike DiTroia"]


def test_the_check_is_case_and_whitespace_insensitive():
    content = _content(
        [{"type": "person", "value": {"text": "  cigna "}}],
        [{"type": "org", "name": "CIGNA"}],
    )
    assert drop_placeholder_and_self_person_patches(content)["patches"] == []


def test_any_non_person_type_counts_not_only_org():
    """A project or deliverable named like a person is the same mistake.
    The rule is that the model contradicted itself, not that org is a
    special word."""
    for other in ("project", "deliverable", "artifact"):
        content = _content(
            [{"type": "person", "value": {"text": "Kore"}}],
            [{"type": other, "name": "Kore"}],
        )
        assert drop_placeholder_and_self_person_patches(content)["patches"] == [], other


def test_a_person_also_named_as_a_person_entity_survives():
    """The common case must not regress: most person patches have a
    matching person entity and that is agreement, not contradiction."""
    content = _content(
        [{"type": "person", "value": {"text": "Pallavi"}}],
        [{"type": "person", "name": "Pallavi"}],
    )
    kept = drop_placeholder_and_self_person_patches(content)["patches"]
    assert len(kept) == 1


def test_no_entities_array_changes_nothing():
    content = _content([{"type": "person", "value": {"text": "Denby"}}])
    assert len(drop_placeholder_and_self_person_patches(content)["patches"]) == 1


def test_non_person_patches_are_untouched():
    content = _content(
        [{"type": "commitment", "value": {"text": "CIGNA demo", "owner": "Mike"}}],
        [{"type": "org", "name": "CIGNA"}],
    )
    assert len(drop_placeholder_and_self_person_patches(content)["patches"]) == 1
