"""A declared kind CQ does not know must be AUDIBLE, not just harmless.

Doc 22 resolves an unrecognised `material_kind` to `meeting` on purpose: a
client sending a kind CQ has not shipped yet must get today's behavior
rather than lose its meeting. That ruling stands. What this file pins is
the other half, which the ruling does not give you for free.

From the OUTCOME, three inputs are identical: a flag that never left the
client, a flag dropped on a hop, and `"listenting"`. All three extract as a
meeting. GhostPour's request-side proof (2026-09-03) established that they
forward the value untouched, no trim, no lowercase, no default, so the wire
CAN tell them apart. CQ's log could not, and the doc 22 acceptance test is
read off CQ's log. That is the gap.

Doc 19.10: an absence is evidence only if the contradicting result had a
channel to arrive through. This is the channel.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from contextquilt.services import material_kind as mk


# ----------------------------------------------------------------------
# What resolves, which is GhostPour's five cases plus the variants they
# deliberately do NOT normalise, so CQ must.
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("listening", True),
        ("Listening", True),
        ("  listening  ", True),
        ("LISTENING", True),
        ("meeting", False),
        ("  Listenting  ", False),   # GP's line 3: the typo, as sent
        ("podcast", False),
        ("", False),
    ],
)
def test_resolution_is_case_and_whitespace_insensitive(raw, expected):
    """GP passes the value through untouched, so normalising is CQ's job.

    If this ever regresses to an exact match, a capitalised or padded
    `listening` becomes a meeting and looks exactly like a dropped flag.
    """
    assert mk.is_listening({"material_kind": raw}) is expected


def test_absent_and_malformed_are_meeting():
    assert mk.is_listening({}) is False
    assert mk.is_listening(None) is False
    assert mk.is_listening({"material_kind": None}) is False
    assert mk.is_listening({"material_kind": 7}) is False


# ----------------------------------------------------------------------
# What is worth SAYING, which is the point of the change
# ----------------------------------------------------------------------

def test_absent_is_silent_because_absent_is_not_a_mistake():
    """Every existing caller sends nothing. A warning per ingest would be
    noise that trains the reader to ignore the line that matters."""
    assert mk.unrecognised_kind({}) is None
    assert mk.unrecognised_kind(None) is None
    assert mk.unrecognised_kind({"material_kind": None}) is None


@pytest.mark.parametrize("raw", ["listening", "meeting", "  Listening  ", "MEETING"])
def test_a_value_that_resolved_is_silent(raw):
    assert mk.unrecognised_kind({"material_kind": raw}) is None


@pytest.mark.parametrize("raw", ["listenting", "  Listenting  ", "podcast", "watching", ""])
def test_a_value_that_did_not_resolve_is_reported_verbatim(raw):
    """Verbatim, because the value IS the diagnosis.

    A normalised or truncated-to-nothing report would tell the reader a
    kind was rejected without telling them which, and the whole reason
    this line exists is to hand them the typo.
    """
    assert mk.unrecognised_kind({"material_kind": raw}) == raw


def test_a_non_string_is_reported_rather_than_swallowed():
    """A client sending the wrong TYPE is the same class of mistake."""
    assert mk.unrecognised_kind({"material_kind": 7}) == "7"
    assert mk.unrecognised_kind({"material_kind": ["listening"]}) == "['listening']"


def test_the_report_is_bounded():
    """A hostile or buggy client must not put a megabyte in the log."""
    got = mk.unrecognised_kind({"material_kind": "x" * 5000})
    assert got is not None and len(got) <= 5000


# ----------------------------------------------------------------------
# The pair, stated as the thing the next person needs
# ----------------------------------------------------------------------

def test_the_typo_and_the_dropped_flag_are_now_distinguishable():
    """The whole change in one assertion.

    Both resolve to `meeting`. Only one of them says anything. Before
    this, CQ's log could not tell a doc 22 acceptance failure caused by
    a client typo from one caused by a hop that never forwarded the
    field, and somebody would have spent an afternoon on the wrong one.
    """
    typo = {"material_kind": "listenting"}
    dropped: dict = {}

    assert mk.is_listening(typo) == mk.is_listening(dropped) is False
    assert mk.unrecognised_kind(typo) == "listenting"
    assert mk.unrecognised_kind(dropped) is None
