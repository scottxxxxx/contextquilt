"""A replayed ingest says so, and a first delivery says nothing.

ShoulderSurf writes `pending_ingests.json` at session start and clears
it only on a 2xx, so a live meeting's ingest is a durable debt replayed
later with `X-CZ-Recovery` set. CQ ignored the header entirely until
2026-09-10, and GhostPour never forwarded it until GP #954, so the
stream's 61 byte-identical repeat origins (86 entries in May, 33 in
September, most recent 2026-09-03) cannot be told apart from deliberate
re-ingests. The sender was labelling them the whole time.

The negative cases carry the weight here. A stamp that appears when it
should not is worse than no stamp at all, because a duplicate wearing an
authoritative label stops being questioned.
"""

import pytest

from contextquilt.services.ingest_markers import (
    MAX_MARKER_CHARS, recovery_marker, stamp_recovery,
)


def test_a_replay_is_stamped():
    payload = {"user_id": "u1"}
    assert stamp_recovery(payload, "pending-ingest") == "pending-ingest"
    assert payload["recovery"] == "pending-ingest"


def test_an_ordinary_delivery_leaves_the_payload_untouched():
    """ABSENT MEANS ABSENT. A key that is always present and sometimes
    null would put a recovery field on every stream entry, and which
    entries carry one is precisely the thing being measured."""
    payload = {"user_id": "u1"}
    assert stamp_recovery(payload, None) is None
    assert "recovery" not in payload
    assert payload == {"user_id": "u1"}


@pytest.mark.parametrize("header", ["", "   ", "\t\n", None, 0, False, [], {}, 1])
def test_nothing_that_is_not_a_real_marker_ever_stamps(header):
    """A header that arrived but says nothing is not evidence of a
    replay. Non-strings included: a proxy that forwards oddly must not
    be able to mint a label by sending a truthy non-string."""
    payload = {"user_id": "u1"}
    assert stamp_recovery(payload, header) is None
    assert "recovery" not in payload


def test_a_long_marker_is_truncated():
    """Unvalidated client text that lands in a payload the worker parses
    and persists on the stream for months."""
    payload = {}
    marker = stamp_recovery(payload, "x" * 500)
    assert len(marker) == MAX_MARKER_CHARS
    assert payload["recovery"] == "x" * MAX_MARKER_CHARS


def test_surrounding_whitespace_is_not_part_of_the_marker():
    payload = {}
    assert stamp_recovery(payload, "  replay  ") == "replay"
    assert payload["recovery"] == "replay"


def test_the_marker_helper_agrees_with_the_stamp():
    """One rule. If these ever disagree, the log line and the stored
    entry would say different things about the same delivery."""
    for header in ("replay", "  replay  ", "", None, "y" * 200, 5):
        payload = {}
        assert stamp_recovery(payload, header) == recovery_marker(header)


def test_it_does_not_disturb_the_rest_of_the_payload():
    payload = {"user_id": "u1", "metadata": {"origin_id": "M1"},
               "content": "transcript"}
    stamp_recovery(payload, "replay")
    assert payload["user_id"] == "u1"
    assert payload["metadata"] == {"origin_id": "M1"}
    assert payload["content"] == "transcript"


def test_stamping_twice_is_idempotent():
    """A retry of the retry must not compound."""
    payload = {}
    stamp_recovery(payload, "replay")
    stamp_recovery(payload, "replay")
    assert payload == {"recovery": "replay"}
