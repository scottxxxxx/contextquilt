"""One name rule on the read side, across its four former carriers.

Doc 25 finding 2. Until 2026-09-13 "is this row about that person" was
decided four different ways: the query matcher on word boundaries, the
scorer's text boost by bare substring, the scorer's owner boost by first
token, and the formatter's capsule fold by first token under a docstring
that said "when one side is a bare first name" and did not check it.

These tests EXECUTE the scorer and the formatter rather than reading
them, because the defect was in what a comparison did and not in whether
it was present.
"""

import json
from datetime import datetime

from src.contextquilt.services.entity_match import name_in_text, same_person
from src.contextquilt.services.recall_formatter import format_flat_ranked_served
from src.contextquilt.services.recall_scorer import score_patches


# --------------------------------------------------------------------
# The rule itself
# --------------------------------------------------------------------

def test_same_person_equal_names():
    assert same_person("Steven Williams", "steven williams")


def test_same_person_bare_first_name_reaches_full_name_both_ways():
    # "Steven" in the query reaches a row owned by "Steven Williams".
    assert same_person("Steven Williams", "Steven")
    assert same_person("Steven", "Steven Williams")


def test_two_full_names_sharing_a_first_token_are_two_people():
    """THE DEFECT. Both old bodies said these were the same person."""
    assert not same_person("Steven Levy", "Steven Williams")
    assert not same_person("Steven Williams", "Steven Levy")


def test_same_person_never_a_substring():
    assert not same_person("Al", "Alan Turing")      # bare, but not the first token
    assert not same_person("Sam", "Samantha Ortiz")
    assert not same_person("", "Steven")
    assert not same_person("Steven", "")


def test_name_in_text_is_word_bounded_like_the_query_matcher():
    assert name_in_text("rv", "the rv is parked outside")
    assert not name_in_text("rv", "we scheduled the interview for monday")
    assert not name_in_text("al", "we also allow all of it")
    assert name_in_text("al", "ask al about it")


def test_name_in_text_keeps_the_query_matchers_joiner_rule():
    # "don't" must not summon Don; the query side made that call and
    # the patch side must not disagree with it.
    assert not name_in_text("don", "i don't think so")
    assert name_in_text("don", "don said no")


# --------------------------------------------------------------------
# The scorer's text boost
# --------------------------------------------------------------------

def _patch(pid, ptype, text, owner=None):
    return {
        "patch_id": pid, "patch_type": ptype,
        "value": json.dumps({"text": text, "owner": owner}),
        "created_at": datetime.utcnow(), "last_observed_at": None,
    }


def test_entity_boost_does_not_fire_inside_another_word():
    """Once "RV" is legitimately matched in the query, a row about an
    interview must not take the +100 boost for containing the letters."""
    patches = [
        _patch("inside", "takeaway", "we scheduled the interview for monday"),
        _patch("real", "takeaway", "the rv needs new tires"),
    ]
    scored = score_patches(patches, "what about the rv", ["RV"])
    by_id = {row["patch_id"]: s for s, row in scored}
    assert by_id["real"] - by_id["inside"] >= 100.0, (
        "the substring boost fired on 'interview'")


def test_entity_boost_still_fires_on_a_whole_word():
    patches = [_patch("hit", "takeaway", "sync with ProjectX on friday")]
    boosted = score_patches(patches, "status of ProjectX", ["ProjectX"])[0][0]
    plain = score_patches(patches, "status of ProjectX", [])[0][0]
    assert boosted - plain >= 100.0


# --------------------------------------------------------------------
# The scorer's owner boost (conduct rows)
# --------------------------------------------------------------------

def test_conduct_owner_boost_does_not_reach_a_namesake():
    """A conduct row owned by Steven Levy is not about Steven Williams."""
    patches = [
        _patch("namesake", "behavior", "asked a probing question", owner="Steven Levy"),
        _patch("theperson", "behavior", "asked a probing question", owner="Steven Williams"),
    ]
    scored = score_patches(
        patches, "how does steven williams operate", ["Steven Williams"],
        conduct_types=frozenset({"behavior"}),
    )
    by_id = {row["patch_id"]: s for s, row in scored}
    assert by_id["theperson"] - by_id["namesake"] >= 100.0


def test_conduct_owner_boost_still_reaches_a_bare_first_name():
    patches = [_patch("row", "behavior", "moved the agenda", owner="Steven Williams")]
    boosted = score_patches(patches, "steven?", ["Steven"],
                            conduct_types=frozenset({"behavior"}))[0][0]
    plain = score_patches(patches, "steven?", [],
                          conduct_types=frozenset({"behavior"}))[0][0]
    assert boosted - plain >= 100.0


# --------------------------------------------------------------------
# The formatter's capsule fold
# --------------------------------------------------------------------

def _entity(name, etype="person"):
    return {"name": name, "entity_type": etype, "description": None}


def test_capsule_does_not_fold_a_namesakes_conduct_into_the_header():
    scored = [
        (90.0, _patch("levy", "behavior", "interrupted the demo twice to ask about pricing",
                      owner="Steven Levy")),
        (80.0, _patch("williams", "behavior", "waited for the whole room before deciding",
                      owner="Steven Williams")),
    ]
    context, _, served = format_flat_ranked_served(
        scored, entity_rows=[_entity("Steven Williams")], relationship_rows=[],
        conduct_types=frozenset({"behavior"}),
    )
    header = context.split("\n\n")[0]
    assert "waited for the whole room" in header
    assert "interrupted the demo" not in header, "the namesake's conduct folded in"
    # And the namesake's row keeps its own place in the list.
    assert "[behavior] interrupted the demo" in context
    assert served == ["levy", "williams"]
