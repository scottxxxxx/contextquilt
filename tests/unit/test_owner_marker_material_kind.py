"""Material the user was not in gets no `(you)` marker.

#414 put every doc 22 suppression in one block after the extraction
prompt is resolved. The owner marker is injected EIGHTY LINES EARLIER,
so a caller sending `material_kind: "listening"` with an owner label
would have had "(you)" written into a recording the user was never in.

That is the one assertion the whole design exists to prevent: doc 22
exists because a podcast minted person entities and speaker appearances
asserting Scott had been in a room with its hosts.

It never fired, because nobody has ever sent the flag. It was found by
ShoulderSurf asking what would happen to an IMPORTED meeting, which is
the same shape: material whose participants are not the account owner,
where their client would otherwise send the IMPORTER's name as the owner
label and, on a same-first-name collision, make the importer the owner of
somebody else's words.

ENFORCED AT THE READER, not promised by the caller. The honest client
behaviour is to send no label for such material and ShoulderSurf will,
but a suppression that depends on every caller remembering is the shape
this codebase keeps paying for.
"""

from __future__ import annotations

import ast
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKER_SRC = (ROOT / "src" / "worker.py").read_text()
sys.path.insert(0, str(ROOT / "src"))

from contextquilt.services import material_kind as real_material_kind  # noqa: E402


def _handler_source() -> str:
    tree = ast.parse(WORKER_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_meeting_summary":
            seg = ast.get_source_segment(WORKER_SRC, node)
            assert seg
            return textwrap.dedent(seg)
    raise AssertionError("handle_meeting_summary not found")


def _ordering() -> tuple[int, int]:
    """Where the kind is consulted, and where the marker is applied."""
    body = _handler_source()
    return (
        body.index("material_kind.is_listening(metadata) and owner_speaker_label"),
        body.index("effective_summary = normalize_owner_in_transcript("),
    )


def test_the_kind_is_consulted_BEFORE_the_marker_is_applied():
    """The whole defect was ordering, so the ordering is the test.

    A check that runs after `normalize_owner_in_transcript` cannot
    un-inject a marker; the transcript already carries it and every
    downstream stage reads the rewritten text.
    """
    kind_at, marker_at = _ordering()
    assert kind_at < marker_at, (
        "material_kind is consulted after the marker is applied, which is "
        "the original bug: the transcript is already rewritten by then"
    )


def test_the_suppression_nulls_the_label_rather_than_skipping_the_call():
    """`normalize_owner_in_transcript` still runs, with no label.

    Skipping the call entirely would also skip whatever else it
    normalises, and the goal is narrow: do not claim an owner. Nulling
    the label is the smallest change that achieves it.
    """
    body = _handler_source()
    window = body[_ordering()[0]:_ordering()[1]]
    assert "owner_speaker_label = None" in window
    assert "normalize_owner_in_transcript" in body


def test_the_suppression_is_audible():
    """An ignored client field must say so.

    A label that is silently dropped is indistinguishable from a label
    that was never sent, and the client would have no way to learn that
    its owner label is not being honoured.
    """
    body = _handler_source()
    assert "owner_label_ignored_for_material_kind" in body
    window = body[_ordering()[0]:_ordering()[1]]
    assert "material_kind=" in window, "the log does not say which kind suppressed it"
    assert "label=" in window, "the log does not say which label was ignored"


# ----------------------------------------------------------------------
# The predicate the gate resolves through, exercised for real
# ----------------------------------------------------------------------

@pytest.mark.parametrize("metadata,suppressed", [
    ({"material_kind": "listening"}, True),
    ({"material_kind": "Listening"}, True),
    ({"material_kind": "  listening  "}, True),
    ({"material_kind": "meeting"}, False),
    ({"material_kind": "listenting"}, False),   # a typo is a meeting, by design
    ({}, False),
    (None, False),
])
def test_the_gate_fires_on_exactly_the_kinds_it_should(metadata, suppressed):
    """A real meeting must keep its marker. That is the common case and
    breaking it would strip the user's identity from every ingest."""
    assert real_material_kind.is_listening(metadata) is suppressed


def test_an_unrecognised_kind_keeps_todays_behavior():
    """Doc 22's ruling: absent and unrecognised both mean `meeting`.

    A client sending a kind CQ has not shipped yet must not silently lose
    its owner marker, which would be a worse failure than the one this
    file fixes.
    """
    assert real_material_kind.is_listening({"material_kind": "secondhand"}) is False
