"""A meeting the account owner was not in. Keep the people, claim no presence.

A colleague forwards you the meeting you missed and you import it.
ShoulderSurf stopped before building the import prompt because their
capture path sends the enrolled voice profile of the CAPTURING device as
the owner label, so an import would have asserted an owner who was never
in the room, and on a shared first name would have made the importer the
owner of somebody else's words.

Scott ruled the shape on 2026-09-04 in one sentence: "keep the people,
just don't let it claim I was there."

THAT SENTENCE IS THE WHOLE SPEC AND IT SPLITS THIS KIND FROM `listening`.
A podcast's speakers are strangers, so doc 22 empties the entity array. A
forwarded meeting is full of the user's actual colleagues, and the useful
thing about it is precisely that Vijay was in it and what he pushed back
on. Emptying that would be wrong rather than merely conservative.

So the two non-participation kinds agree on exactly one property,
presence, and disagree on nearly everything else. The tests below assert
both halves, because a change that made `secondhand` behave like
`listening` would look like consistency and would delete the reason the
kind exists.
"""

from __future__ import annotations

import ast
import copy
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKER_SRC = (ROOT / "src" / "worker.py").read_text()
sys.path.insert(0, str(ROOT / "src"))

from contextquilt.services import material_kind as mk  # noqa: E402

SECONDHAND = {"material_kind": "secondhand"}
LISTENING = {"material_kind": "listening"}
MEETING = {"material_kind": "meeting"}

# What a forwarded meeting actually produces: colleagues doing things,
# plus the self-description the model invents when no speaker is marked.
CONTENT = {
    "patches": [
        {"type": "trait", "value": {"text": "Thinks in systems"}},
        {"type": "preference", "value": {"text": "Prefers async standups"}},
        {"type": "behavior", "value": {"text": "Asked for the cost breakdown "
                                               "before agreeing", "owner": "Vijay"}},
        {"type": "takeaway", "value": {"text": "Churn risk is real above 12%"}},
        {"type": "person", "value": {"text": "Vijay leads the platform team"}},
        {"type": "decision", "value": {"text": "Launch moves to March"}},
    ],
    "entities": [{"name": "Vijay", "type": "person"}],
    "relationships": [{"from": "Vijay", "to": "platform", "type": "works_on"}],
}


def _handler_source() -> str:
    tree = ast.parse(WORKER_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_meeting_summary":
            return textwrap.dedent(ast.get_source_segment(WORKER_SRC, node) or "")
    raise AssertionError("handle_meeting_summary not found")


# ----------------------------------------------------------------------
# "don't let it claim I was there"
# ----------------------------------------------------------------------

@pytest.mark.parametrize("metadata", [SECONDHAND, LISTENING])
def test_neither_non_participation_kind_claims_presence(metadata):
    assert mk.claims_user_presence(metadata) is False


def test_a_normal_meeting_still_claims_presence():
    """The common case. Breaking this strips the user's identity from
    every ingest, which is far worse than the bug being fixed."""
    assert mk.claims_user_presence(MEETING) is True
    assert mk.claims_user_presence({}) is True
    assert mk.claims_user_presence(None) is True


def test_an_unrecognised_kind_still_claims_presence():
    """Doc 22's standing ruling: absent and unrecognised both mean
    `meeting`. A client sending a kind CQ has not shipped must not
    silently lose its owner marker."""
    assert mk.claims_user_presence({"material_kind": "secondhandd"}) is True


def test_the_marker_gate_reads_the_shared_predicate_not_one_kind():
    """It was `is_listening` when it shipped. If it stays that way, the
    new kind silently gets a `(you)` marker, which is the exact claim
    Scott ruled out."""
    body = _handler_source()
    assert "material_kind.claims_user_presence(metadata)" in body
    gate = body[body.index("owner_label_ignored_for_material_kind") - 800:
                body.index("owner_label_ignored_for_material_kind")]
    assert "is_listening" not in gate, (
        "the marker gate is still keyed on one kind rather than on presence"
    )


def test_the_open_commitments_block_is_withheld():
    """A meeting the user was not in cannot close the user's commitment,
    and handing the model the list invites it to try."""
    body = _handler_source()
    block = body[body.index("open_commits_block = ("):
                 body.index("open_commits_block = (") + 400]
    assert "claims_user_presence" in block
    assert 'else ""' in block


# ----------------------------------------------------------------------
# "keep the people"
# ----------------------------------------------------------------------

def test_secondhand_keeps_the_people_and_what_they_did():
    """The half that separates this kind from `listening`.

    If this ever fails, the kind has collapsed into the podcast path and
    a forwarded meeting stops being worth importing.
    """
    out = mk.strip_self_disclosure(copy.deepcopy(CONTENT), SECONDHAND)
    kept = [p["type"] for p in out["patches"]]

    assert "behavior" in kept, "the conduct of the people in the room was dropped"
    assert "person" in kept, "the people themselves were dropped"
    assert "takeaway" in kept
    assert "decision" in kept
    assert out["entities"], "the entity array was emptied; that is the listening rule"
    assert out["relationships"], "relationships were emptied"


def test_secondhand_drops_only_the_self_disclosed_types():
    out = mk.strip_self_disclosure(copy.deepcopy(CONTENT), SECONDHAND)
    kept = [p["type"] for p in out["patches"]]

    assert "trait" not in kept
    assert "preference" not in kept
    assert out["_self_disclosure_stripped"]["count"] == 2
    assert out["_self_disclosure_stripped"]["kind"] == "secondhand"


def test_a_normal_meeting_keeps_its_self_disclosure():
    """Traits and preferences are the point of a real meeting."""
    out = mk.strip_self_disclosure(copy.deepcopy(CONTENT), MEETING)

    assert [p["type"] for p in out["patches"]] == [p["type"] for p in CONTENT["patches"]]
    assert "_self_disclosure_stripped" not in out


def test_the_strip_is_reported_not_silent():
    """An ingest that quietly produces fewer patches than the model
    returned is indistinguishable from a model that returned fewer."""
    out = mk.strip_self_disclosure(copy.deepcopy(CONTENT), SECONDHAND)
    report = out["_self_disclosure_stripped"]

    assert {d["type"] for d in report["dropped"]} == {"trait", "preference"}
    assert all(d["text"] for d in report["dropped"]), "dropped rows carry no text"


@pytest.mark.parametrize("content", [None, [], "nonsense", {}, {"patches": "no"}])
def test_malformed_content_never_raises(content):
    """This runs inside ingest, which must never lose a meeting to a
    malformed model answer."""
    mk.strip_self_disclosure(content, SECONDHAND)


# ----------------------------------------------------------------------
# The two kinds are not the same kind
# ----------------------------------------------------------------------

def test_listening_and_secondhand_agree_on_presence_and_differ_on_people():
    """Stated as one assertion because it is the design.

    A future change that unified them would look like tidying and would
    delete the distinction Scott actually ruled on.
    """
    assert mk.claims_user_presence(LISTENING) == mk.claims_user_presence(SECONDHAND)

    # `listening` has its own sanitizer that empties the people entirely.
    listening_out = mk.sanitize_listening_patches(
        copy.deepcopy(CONTENT), mk.allowed_types(None))
    assert listening_out["entities"] == []

    secondhand_out = mk.strip_self_disclosure(copy.deepcopy(CONTENT), SECONDHAND)
    assert secondhand_out["entities"], "secondhand must not empty the entity array"


def test_secondhand_is_in_the_known_vocabulary():
    """Or it resolves to `meeting` and every suppression above is inert,
    which is exactly how it behaved for the first ten minutes of being
    written."""
    assert "secondhand" in mk.KNOWN_KINDS
    assert mk.from_metadata(SECONDHAND) == "secondhand"
    assert mk.unrecognised_kind(SECONDHAND) is None


# ----------------------------------------------------------------------
# The variants ShoulderSurf's client test mirrors
# ----------------------------------------------------------------------

#: Kept as an explicit table because ShoulderSurf's `survivesResolver`
#: test carries THIS LIST, copied from a measurement run against the
#: deployed resolver on 2026-09-04. Their copy is a mirror; this is the
#: original.
#:
#: That distinction is the reason this block exists. Their test asserts
#: their own constant against their own list, so if the resolver here
#: ever stopped stripping and lowercasing, their test would stay GREEN
#: while the contract moved underneath it, and the first symptom would
#: be a forwarded meeting extracting as a first-hand one. A mirror
#: cannot notice the original changing. The original has to fail first,
#: which is what these two cases are for.
RESOLVES = ["secondhand", "Secondhand", "SECONDHAND", "  secondhand  ", "secondHand"]
FALLS_THROUGH = ["second_hand", "second-hand", "second hand", "secondhandd"]


@pytest.mark.parametrize("value", RESOLVES)
def test_the_variants_shouldersurf_pinned_still_resolve(value):
    """If this goes red, tell ShoulderSurf before shipping.

    Their client test is a copy of this list and will not notice.
    """
    assert mk.from_metadata({"material_kind": value}) == mk.SECONDHAND
    assert mk.claims_user_presence({"material_kind": value}) is False


@pytest.mark.parametrize("value", FALLS_THROUGH)
def test_a_separator_falls_through_and_says_so(value):
    """`second_hand` is what somebody tidying up snake_case reaches for.

    It resolves to `meeting`, which is the deliberate ruling, and the
    only thing that separates it from a key that never arrived is the
    warning. ShoulderSurf made the value a named constant on their side
    for exactly this; the warning is the half on this side.
    """
    assert mk.from_metadata({"material_kind": value}) == mk.MEETING
    assert mk.unrecognised_kind({"material_kind": value}) == value
