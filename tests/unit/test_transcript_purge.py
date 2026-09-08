"""The transcript sweep: what it matches, and everything it must not.

Scott ruled on 2026-09-07 that a deleted meeting takes its transcript
and a deleted project takes its meetings' transcripts. Until then the
`memory_updates` stream had never been trimmed: 1,477 entries, first one
2026-03-22, and only an account purge had ever deleted one.

These tests lean hard on the NEGATIVE cases, because the operation is an
XDEL of the user's own recordings and there is no undo. Every way this
could match too much is worth more here than the happy path.
"""

import json

import pytest

from contextquilt.services.transcript_purge import (
    entry_matches, entry_origin, sweep,
)

USER = "fa4d903c-24c0-45d5-9fdb-b5496e32501b"
OTHER_USER = "00000000-0000-0000-0000-000000000999"
MEETING = "BCF304AB-0000-0000-0000-00000000000A"
PROJECT = "8DEBE602-0000-0000-0000-000000000001"


def _entry(user=USER, origin=MEETING, project=PROJECT, **extra):
    meta = {}
    if origin is not None:
        meta["origin_id"] = origin
    if project is not None:
        meta["project_id"] = project
    return json.dumps({"user_id": user, "content": "transcript body",
                       "metadata": meta, **extra})


class FakeRedis:
    """Minimal XRANGE/XDEL. Real enough to exercise the cursor loop."""

    def __init__(self, entries):
        self.entries = list(entries)   # [(id, {"data": ...})]
        self.deleted = []

    async def xrange(self, key, min="-", count=None):
        start = 0
        if min != "-":
            after = min[1:] if min.startswith("(") else min
            start = next((i for i, (eid, _) in enumerate(self.entries)
                          if eid > after), len(self.entries))
        return self.entries[start:start + (count or len(self.entries))]

    async def xdel(self, key, *ids):
        self.deleted.extend(ids)
        self.entries = [(i, f) for i, f in self.entries if i not in set(ids)]
        return len(ids)


def test_a_meeting_entry_matches_by_origin():
    assert entry_matches(_entry(), USER, origin_ids={MEETING}) is True


def test_another_users_entry_never_matches():
    """The single worst failure available here."""
    assert entry_matches(_entry(user=OTHER_USER), USER,
                         origin_ids={MEETING}) is False
    assert entry_matches(_entry(user=OTHER_USER), USER,
                         project_id=PROJECT) is False


def test_an_unparseable_entry_never_matches():
    """Deleting on a parse guess is worse than leaving a row nobody can
    read. Same rule account_purge.stream_entry_is_users follows."""
    for raw in ("", None, "{not json", b"\x00\x01", "[]", "null"):
        assert entry_matches(raw, USER, origin_ids={MEETING}) is False


def test_an_empty_scope_matches_nothing():
    """An empty scope must clear NOTHING rather than everything, the same
    failure direction #466 chose for its delete flag."""
    assert entry_matches(_entry(), USER) is False
    assert entry_matches(_entry(), USER, origin_ids=set()) is False
    assert entry_matches(_entry(), USER, project_id=None) is False


def test_a_different_meeting_does_not_match():
    assert entry_matches(_entry(origin="OTHER-MEETING"), USER,
                         origin_ids={MEETING}) is False


def test_the_project_fallback_catches_an_unassigned_meeting():
    """24 entries on prod sit against projects that are already archived
    with no assignment rows at all. Without this leg they are
    unreachable forever."""
    entry = _entry(origin="MEETING-NOBODY-ASSIGNED", project=PROJECT)
    assert entry_matches(entry, USER, origin_ids=set(), project_id=PROJECT) is True


def test_the_project_fallback_does_not_cross_projects():
    entry = _entry(origin="M2", project="SOME-OTHER-PROJECT")
    assert entry_matches(entry, USER, origin_ids={MEETING},
                         project_id=PROJECT) is False


def test_an_entry_with_neither_origin_nor_project_is_unreachable():
    """A quarter of the stream is in this state and this test records
    that as intended behaviour rather than an oversight: only an account
    purge clears them."""
    assert entry_origin(_entry(origin=None, project=None), USER) is None
    assert entry_matches(_entry(origin=None, project=None), USER,
                         origin_ids={MEETING}, project_id=PROJECT) is False


def test_a_hydrate_marker_has_no_metadata_and_is_left_alone():
    raw = json.dumps({"type": "hydrate", "user_id": USER,
                      "timestamp": "2026-09-07T00:00:00"})
    assert entry_matches(raw, USER, origin_ids={MEETING}) is False


@pytest.mark.asyncio
async def test_sweep_without_apply_deletes_nothing():
    """The preview must be able to run on live data with no risk."""
    r = FakeRedis([("1-0", {"data": _entry()}), ("2-0", {"data": _entry()})])
    out = await sweep(r, USER, origin_ids=[MEETING], apply=False)
    assert out["matched"] == 2
    assert out["deleted"] == 0
    assert r.deleted == []
    assert len(r.entries) == 2


@pytest.mark.asyncio
async def test_sweep_with_apply_deletes_only_the_matches():
    r = FakeRedis([
        ("1-0", {"data": _entry()}),
        ("2-0", {"data": _entry(user=OTHER_USER)}),
        ("3-0", {"data": _entry(origin="OTHER-MEETING", project=None)}),
        ("4-0", {"data": _entry()}),
    ])
    out = await sweep(r, USER, origin_ids=[MEETING], apply=True)
    assert out["matched"] == 2
    assert out["deleted"] == 2
    assert sorted(r.deleted) == ["1-0", "4-0"]
    assert [i for i, _ in r.entries] == ["2-0", "3-0"]


@pytest.mark.asyncio
async def test_the_preview_count_equals_what_the_delete_removes():
    """#466's rule: the number a user is warned with cannot come from a
    different query than the one that deletes."""
    entries = [(f"{i}-0", {"data": _entry()}) for i in range(1, 8)]
    preview = await sweep(FakeRedis(entries), USER, origin_ids=[MEETING],
                          apply=False)
    r = FakeRedis(entries)
    applied = await sweep(r, USER, origin_ids=[MEETING], apply=True)
    assert preview["matched"] == applied["deleted"] == 7


@pytest.mark.asyncio
async def test_the_cursor_advances_past_a_full_batch():
    """A batch boundary that does not advance is an infinite loop on a
    route holding a user's delete."""
    import contextquilt.services.transcript_purge as tp
    entries = [(f"{i:04d}-0", {"data": _entry()}) for i in range(1, 12)]
    original = tp.STREAM_SCAN_BATCH
    tp.STREAM_SCAN_BATCH = 5
    try:
        r = FakeRedis(entries)
        out = await sweep(r, USER, origin_ids=[MEETING], apply=True)
    finally:
        tp.STREAM_SCAN_BATCH = original
    assert out["matched"] == 11
    assert out["deleted"] == 11
    assert r.entries == []


@pytest.mark.asyncio
async def test_bytes_are_reported_for_the_dialog():
    r = FakeRedis([("1-0", {"data": _entry()})])
    out = await sweep(r, USER, origin_ids=[MEETING], apply=False)
    assert out["bytes"] == len(_entry())
