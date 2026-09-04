"""A behavior row whose owner is the OBJECT of its own sentence.

"Asked Vijay to have Arnav join the Tuesday enablement call" stamped
`owner="Vijay"` says Vijay asked himself. The actor is whoever was
speaking, which the row does not record, so the observation is real and
its attribution is not, and it renders on Vijay's page as his conduct.

Measured on prod 2026-09-03: 33 of 931 active behavior rows name their
own owner and 28 name them mid-sentence. Every one of the 28 inspected
was the user's action filed as the counterparty's. The fixtures below are
real rows, verbatim, not invented shapes.

Doc 16 5.13: a served name may assert only what was OBSERVED. This is the
sharpest available violation of that, because it does not merely overstate
a claim, it inverts who acted.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from contextquilt.services.extraction_schema import (  # noqa: E402
    owner_named_as_object,
    sanitize_behavior_observations,
)


# Verbatim from prod, 2026-09-03.
INVERTED = [
    ("Asked Vijay to have Arnav join the Tuesday enablement call to coordinate "
     "meetings with the product team", "Vijay"),
    ("Asked Sukumar to pull up the agent card so they could look at it", "Sukumar"),
    ("Asked Mike to clarify what the three FIS issues were because he could not "
     "understand them from the explanation given", "Mike"),
    ("Offered to help Steven Williams present the product to Steven Williams's "
     "mother in a specific way", "Steven Williams"),
    ("Asked Reshmi Chakraborty to review one item on the scope with her",
     "Reshmi Chakraborty"),
    ("Asked Joe to have Naveen send the beeline link directly instead of "
     "collecting information himself", "Joe"),
    ("Described specific fixes that Vijay's team made to transcriptions and tone "
     "before offering it for review", "Vijay"),
    ("Requested to see the excel sheet from the team that Satyajit Nanda mentioned",
     "Satyajit Nanda"),
]

# Also verbatim from prod: the owner leads the sentence, so they ARE the
# actor. These must survive untouched.
SUBJECT_INITIAL = [
    ("Pallavi Kandanur provided detailed clarification on incident creation flow "
     "changes", "Pallavi Kandanur"),
    ("Scott asked detailed clarifying questions about the prednisone tapering "
     "schedule", "Scott"),
    ("Ex-husband scrutinizes receipts for expenses and refuses to reimburse items "
     "he deems unnecessary", "Ex-husband"),
]


@pytest.mark.parametrize("text,owner", INVERTED)
def test_the_owner_is_detected_as_the_object(text, owner):
    assert owner_named_as_object(text, owner) is not None


@pytest.mark.parametrize("text,owner", SUBJECT_INITIAL)
def test_a_name_initial_sentence_is_the_subject_and_survives(text, owner):
    """Position is the whole test.

    These sentences are verb-initial by construction because the prompt
    asks for conduct. A name at the FRONT is the actor.
    """
    assert owner_named_as_object(text, owner) is None


def test_a_text_that_never_names_the_owner_is_untouched():
    """The overwhelming majority. 898 of 931 rows on prod."""
    assert owner_named_as_object(
        "Challenged the cost allocation approach on Loomlight", "Mike") is None


def test_matching_is_word_bounded():
    """`Mac` must not fire on `Machine`, or the rule invents inversions."""
    assert owner_named_as_object(
        "Explained how the machine learning pipeline was retrained", "Mac") is None
    assert owner_named_as_object(
        "Asked Mac to re-run the pipeline", "Mac") is not None


def test_a_possessive_still_counts():
    """"Steven Williams's mother" is still Steven Williams as an object."""
    assert owner_named_as_object(
        "Offered to help present the product to Steven Williams's mother",
        "Steven Williams") is not None


@pytest.mark.parametrize("text,owner", [
    (None, "Vijay"), ("Asked Vijay to help", None), ("", "Vijay"),
    ("Asked Vijay to help", ""), ("Asked Vijay to help", "   "), (123, "Vijay"),
])
def test_malformed_input_is_none_never_an_exception(text, owner):
    """This runs inside the ingest sanitizer chain, which must never raise."""
    assert owner_named_as_object(text, owner) is None


# ----------------------------------------------------------------------
# Through the actual sanitizer, which is what ingest calls
# ----------------------------------------------------------------------

def _content(text, owner, ptype="behavior"):
    return {"patches": [{"type": ptype, "value": {"text": text, "owner": owner}}]}


def test_the_sanitizer_drops_an_inverted_row():
    text, owner = INVERTED[0]
    content = sanitize_behavior_observations(_content(text, owner))

    assert content["patches"] == []
    report = content["_behavior_observations_sanitized"]
    assert report["count"] == 1
    assert report["dropped"][0]["reason"] == "owner_is_the_object"


def test_the_sanitizer_keeps_a_subject_initial_row():
    text, owner = SUBJECT_INITIAL[0]
    content = sanitize_behavior_observations(_content(text, owner))

    assert len(content["patches"]) == 1


def test_the_drop_reason_is_its_own_name_not_folded_into_another():
    """`character_not_conduct` and `owner_is_the_object` are different
    defects with different fixes. Folding them would make the counts
    useless for deciding whether either rule is working."""
    text, owner = INVERTED[3]
    content = sanitize_behavior_observations(_content(text, owner))

    reasons = {d["reason"] for d in content["_behavior_observations_sanitized"]["dropped"]}
    assert reasons == {"owner_is_the_object"}


def test_only_behavior_types_are_inspected():
    """A commitment legitimately names the person it is owed to.

    "Send the deck to Vijay" owned by Vijay is a normal commitment, and
    applying this rule to it would delete the ledger.
    """
    content = sanitize_behavior_observations(
        _content("Send the revised deck to Vijay", "Vijay", ptype="commitment"))

    assert len(content["patches"]) == 1
