"""A replayed ingest lands once (Scott's ruling, 2026-09-14).

The route cannot import without fastapi, so the two-POST property is
exercised through `ingest_replay.admit`, the function the route calls,
against an in-memory Redis that implements exactly the five commands the
service uses. The route's wiring is pinned by a source-reading test at
the bottom; the executing half is here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.contextquilt.services.ingest_replay import (
    STREAM_KEY,
    admit,
    already_ingested,
    origins_key,
    payload_origin,
    plan_dedupe,
    remember,
)


class FakeRedis:
    """xadd / xrange / sadd / sismember / xdel, no more."""

    def __init__(self):
        self.streams = {}
        self.sets = {}
        self._ms = 1_700_000_000_000
        self._seq = 0

    async def xadd(self, key, fields):
        self._seq += 1
        entry_id = f"{self._ms}-{self._seq}"
        self.streams.setdefault(key, []).append((entry_id, dict(fields)))
        return entry_id

    @staticmethod
    def _key(entry_id):
        # Redis orders ids numerically by (ms, seq). The first version of
        # this fake compared strings, so "-1000" sorted before "-999" and
        # the exclusive cursor skipped real entries: a fixture bug that
        # read as a service bug for one run.
        ms, _, seq = entry_id.partition("-")
        return int(ms), int(seq or 0)

    async def xrange(self, key, min="-", max="+", count=None):
        entries = self.streams.get(key, [])
        if min != "-":
            exclusive = min.startswith("(")
            bound = self._key(min[1:] if exclusive else min)
            entries = [e for e in entries
                       if (self._key(e[0]) > bound if exclusive else self._key(e[0]) >= bound)]
        return entries[:count] if count else entries

    async def sadd(self, key, *members):
        s = self.sets.setdefault(key, set())
        before = len(s)
        s.update(members)
        return len(s) - before

    async def sismember(self, key, member):
        return member in self.sets.get(key, set())

    async def xdel(self, key, *ids):
        entries = self.streams.get(key, [])
        keep = [e for e in entries if e[0] not in ids]
        removed = len(entries) - len(keep)
        self.streams[key] = keep
        return removed


def _payload(user="u1", origin="m-1", text="hello"):
    return {"user_id": user, "type": "meeting_summary",
            "metadata": {"origin_id": origin, "origin_type": "meeting"},
            "content": text}


async def _post(redis, payload, marker=None):
    """The route's write path in miniature: admit, then xadd + remember."""
    verdict = await admit(redis, payload, marker)
    if verdict["write"]:
        await redis.xadd(STREAM_KEY, {"data": json.dumps(payload)})
        await remember(redis, payload)
    return verdict


# --------------------------------------------------------------------
# The two-POST property
# --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_marked_replay_of_a_landed_origin_writes_nothing():
    """SS's test: same origin twice, marker on the second, ONE entry,
    and the second verdict says what happened."""
    r = FakeRedis()
    first = await _post(r, _payload(), marker=None)
    second = await _post(r, _payload(), marker="pending-ingest-sweep")
    assert first == {"write": True, "deduplicated": False, "origin_id": "m-1"}
    assert second == {"write": False, "deduplicated": True, "origin_id": "m-1"}
    assert len(r.streams[STREAM_KEY]) == 1


@pytest.mark.asyncio
async def test_the_first_bytes_stay():
    """The marker means "do not re-ingest", never "take the newer one"."""
    r = FakeRedis()
    await _post(r, _payload(text="first render"))
    await _post(r, _payload(text="re-rendered"), marker="pending-ingest-sweep")
    (_, fields), = r.streams[STREAM_KEY]
    assert json.loads(fields["data"])["content"] == "first render"


@pytest.mark.asyncio
async def test_an_unmarked_repeat_still_appends():
    """Absent means append: a retry and a deliberate re-send are
    indistinguishable without the label, and guessing is worse."""
    r = FakeRedis()
    await _post(r, _payload())
    verdict = await _post(r, _payload(), marker=None)
    assert verdict["write"] is True
    assert len(r.streams[STREAM_KEY]) == 2


@pytest.mark.asyncio
async def test_a_marked_replay_of_an_origin_that_never_landed_writes():
    """The first delivery failed before its XADD; the replay IS the first."""
    r = FakeRedis()
    verdict = await _post(r, _payload(), marker="pending-ingest-sweep")
    assert verdict["write"] is True
    assert len(r.streams[STREAM_KEY]) == 1


@pytest.mark.asyncio
async def test_dedupe_is_per_user_and_per_origin():
    r = FakeRedis()
    await _post(r, _payload(user="u1", origin="m-1"))
    assert (await _post(r, _payload(user="u2", origin="m-1"), marker="x"))["write"] is True
    assert (await _post(r, _payload(user="u1", origin="m-2"), marker="x"))["write"] is True
    assert (await _post(r, _payload(user="u1", origin="m-1"), marker="x"))["write"] is False
    assert len(r.streams[STREAM_KEY]) == 3


@pytest.mark.asyncio
async def test_a_payload_with_no_origin_always_appends():
    """A replay OF nothing cannot be deduplicated; fail toward append."""
    r = FakeRedis()
    p = {"user_id": "u1", "type": "chat_log", "content": "x"}
    assert (await _post(r, p, marker="x"))["write"] is True
    assert (await _post(r, p, marker="x"))["write"] is True
    assert len(r.streams[STREAM_KEY]) == 2
    assert payload_origin(p) is None


# --------------------------------------------------------------------
# History that predates the SET
# --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_pre_index_entry_is_found_by_the_scan_and_then_indexed():
    r = FakeRedis()
    # Written before the index existed: on the stream, not in the SET.
    await r.xadd(STREAM_KEY, {"data": json.dumps(_payload())})
    assert not await r.sismember(origins_key("u1"), "m-1::meeting_summary")

    assert await already_ingested(r, "u1", "m-1", "meeting_summary") is True
    assert await r.sismember(origins_key("u1"), "m-1::meeting_summary"), \
        "the scan hit was not recorded"

    verdict = await _post(r, _payload(), marker="pending-ingest-sweep")
    assert verdict["write"] is False
    assert len(r.streams[STREAM_KEY]) == 1


@pytest.mark.asyncio
async def test_the_scan_walks_past_one_batch():
    r = FakeRedis()
    for i in range(1200):
        await r.xadd(STREAM_KEY, {"data": json.dumps(_payload(origin=f"m-{i}"))})
    assert await already_ingested(r, "u1", "m-1199", "meeting_summary") is True
    assert await already_ingested(r, "u1", "m-never", "meeting_summary") is False


@pytest.mark.asyncio
async def test_an_unparseable_entry_never_matches():
    r = FakeRedis()
    await r.xadd(STREAM_KEY, {"data": "{not json"})
    await r.xadd(STREAM_KEY, {"data": ""})
    assert await already_ingested(r, "u1", "m-1", "meeting_summary") is False


# --------------------------------------------------------------------
# The one-time cleanup
# --------------------------------------------------------------------

def _e(entry_id, user="u1", origin="m-1", text="x"):
    return (entry_id, json.dumps(_payload(user, origin, text)))


def test_cleanup_keeps_the_latest_and_deletes_the_rest():
    """Identical copies, which is the only case deletion applies to.

    This test used to use three DIFFERENT texts and assert that the
    latest won. That is precisely the behaviour the safety gate now
    refuses, and the fixture was asserting it was safe to throw away two
    transcripts because their stream ids were older. It survives as the
    byte-identical case.
    """
    plan = plan_dedupe([
        _e("1000-0", text="same"),
        _e("2000-0", text="same"),
        _e("3000-0", text="same"),
    ])
    assert plan["keep"] == {("u1", "m-1", "meeting_summary"): "3000-0"}
    assert sorted(plan["delete"]) == ["1000-0", "2000-0"]
    assert plan["ambiguous"] == {}


def test_cleanup_tie_break_is_by_sequence_within_a_millisecond():
    """Same millisecond, ids 1000-2 and 1000-10: numeric, not string order."""
    plan = plan_dedupe([_e("1000-2"), _e("1000-10")])
    assert plan["keep"] == {("u1", "m-1", "meeting_summary"): "1000-10"}
    assert plan["delete"] == ["1000-2"]


def test_cleanup_order_of_input_does_not_matter():
    a = plan_dedupe([_e("1000-0"), _e("3000-0"), _e("2000-0")])
    b = plan_dedupe([_e("3000-0"), _e("1000-0"), _e("2000-0")])
    assert a["keep"] == b["keep"] == {("u1", "m-1", "meeting_summary"): "3000-0"}
    assert sorted(a["delete"]) == sorted(b["delete"]) == ["1000-0", "2000-0"]


def test_cleanup_never_touches_entries_without_an_origin():
    plan = plan_dedupe([
        ("1000-0", json.dumps({"user_id": "u1", "type": "chat_log"})),
        ("2000-0", json.dumps({"user_id": "u1", "type": "chat_log"})),
        ("3000-0", "{not json"),
        _e("4000-0"),
    ])
    assert plan["delete"] == []
    assert plan["keep"] == {("u1", "m-1", "meeting_summary"): "4000-0"}


def test_cleanup_groups_by_user_and_origin_separately():
    plan = plan_dedupe([
        _e("1000-0", user="u1", origin="m-1"),
        _e("2000-0", user="u2", origin="m-1"),
        _e("3000-0", user="u1", origin="m-2"),
        _e("4000-0", user="u1", origin="m-1"),
    ])
    assert plan["delete"] == ["1000-0"]
    assert plan["origins"] == {
        "u1": {"m-1::meeting_summary", "m-2::meeting_summary"},
        "u2": {"m-1::meeting_summary"}}


@pytest.mark.asyncio
async def test_cleanup_plan_applied_leaves_one_entry_and_an_index():
    r = FakeRedis()
    await r.xadd(STREAM_KEY, {"data": json.dumps(_payload(text="same"))})
    await r.xadd(STREAM_KEY, {"data": json.dumps(_payload(text="same"))})
    plan = plan_dedupe([(eid, f["data"]) for eid, f in r.streams[STREAM_KEY]])
    await r.xdel(STREAM_KEY, *plan["delete"])
    for user_id, origins in plan["origins"].items():
        await r.sadd(origins_key(user_id), *origins)
    (_, fields), = r.streams[STREAM_KEY]
    assert json.loads(fields["data"])["content"] == "same"
    assert await r.sismember(origins_key("u1"), "m-1::meeting_summary")


# --------------------------------------------------------------------
# The route is wired to it (source-read: main.py cannot import here)
# --------------------------------------------------------------------

MAIN = (Path(__file__).resolve().parents[2] / "src" / "main.py").read_text()


def _handler() -> str:
    start = MAIN.index('@app.post("/v1/memory"')
    return MAIN[start:MAIN.index('@app.post("/v1/prewarm"')]


def test_the_route_admits_before_it_writes():
    h = _handler()
    assert "await ingest_replay.admit(redis_client, payload, marker)" in h
    assert h.index("ingest_replay.admit(") < h.index("redis_client.xadd(")


def test_a_deduplicated_replay_returns_without_an_xadd_and_says_so():
    h = _handler()
    branch = h[h.index('if not verdict["write"]:'):h.index("# Add to stream")]
    assert "return {" in branch
    assert '"deduplicated": True' in branch
    assert "xadd" not in branch


def test_the_route_indexes_the_origin_after_writing():
    h = _handler()
    assert h.index("redis_client.xadd(") < h.index("ingest_replay.remember(redis_client, payload)")


def test_the_markers_meaning_is_written_where_the_dedupe_lives():
    h = _handler()
    assert "never \"take the\n    newer one\"" in h or 'never "take the newer one"' in h.replace("\n    ", " ")


# --------------------------------------------------------------------
# GhostPour/SS question: do the historical repeats carry the marker?
# --------------------------------------------------------------------

from src.contextquilt.services.ingest_replay import MARKER_STAMPED_SINCE_MS  # noqa: E402


def _e_at(ms, marked=False, origin="m-1"):
    p = _payload(origin=origin)
    if marked:
        p["recovery"] = "pending-ingest-sweep"
    return (f"{ms}-0", json.dumps(p))


def test_marker_stats_count_repeats_not_firsts():
    before = MARKER_STAMPED_SINCE_MS - 1_000_000
    plan = plan_dedupe([_e_at(before), _e_at(before + 1), _e_at(before + 2)])
    assert plan["marker_stats"]["repeats"] == 2


def test_an_unmarked_repeat_after_the_stamp_is_the_finding():
    after = MARKER_STAMPED_SINCE_MS + 1_000_000
    plan = plan_dedupe([
        _e_at(after), _e_at(after + 1, marked=True), _e_at(after + 2, marked=False),
    ])
    s = plan["marker_stats"]
    assert s == {"repeats": 2, "repeats_marked": 1,
                 "repeats_since_marker": 2, "unmarked_since_marker": 1}


def test_a_pre_stamp_unmarked_repeat_is_not_evidence():
    """Before #476 nothing was stamped, so an unmarked May repeat says
    nothing about the sender."""
    before = MARKER_STAMPED_SINCE_MS - 1_000_000
    plan = plan_dedupe([_e_at(before), _e_at(before + 1)])
    s = plan["marker_stats"]
    assert s["repeats"] == 1
    assert s["repeats_since_marker"] == 0
    assert s["unmarked_since_marker"] == 0


def test_the_stamp_instant_is_476s_deploy():
    from datetime import datetime, timezone
    assert MARKER_STAMPED_SINCE_MS == int(
        datetime(2026, 9, 10, 4, 17, 13, tzinfo=timezone.utc).timestamp() * 1000)


# --------------------------------------------------------------------
# The safety gate: only byte-identical copies are ever deleted
# --------------------------------------------------------------------

def _e_text(entry_id, text, user="u1", origin="m-1"):
    return (entry_id, json.dumps(_payload(user, origin, text)))


def test_a_group_whose_copies_differ_is_never_deleted_from():
    """THE CATCH, 2026-09-14. 278 duplicate groups on prod, only 61
    byte-identical, 217 DIFFERING, with sizes like 52,538 against 13,129
    characters for one origin seconds apart. Latest-wins across those
    would have deleted the longer transcript wherever the shorter one
    arrived second, irreversibly, on the only copy."""
    plan = plan_dedupe([
        _e_text("1000-0", "the full forty minute transcript"),
        _e_text("2000-0", "a short one"),
    ])
    assert plan["delete"] == []
    assert list(plan["ambiguous"]) == [("u1", "m-1", "meeting_summary")]
    assert plan["ambiguous"][("u1", "m-1", "meeting_summary")] == ["1000-0", "2000-0"]


def test_identical_copies_are_still_collapsed():
    plan = plan_dedupe([
        _e_text("1000-0", "same text"),
        _e_text("2000-0", "same text"),
    ])
    assert plan["delete"] == ["1000-0"]
    assert plan["ambiguous"] == {}


def test_one_differing_copy_protects_its_whole_group():
    """Three copies, two identical and one not: the group is ambiguous
    and nothing in it is touched. Deleting the 'obvious' pair would
    still be choosing which transcript survives."""
    plan = plan_dedupe([
        _e_text("1000-0", "same text"),
        _e_text("2000-0", "same text"),
        _e_text("3000-0", "different text entirely"),
    ])
    assert plan["delete"] == []
    assert len(plan["ambiguous"][("u1", "m-1", "meeting_summary")]) == 3


def test_ambiguity_is_per_group_not_global():
    plan = plan_dedupe([
        _e_text("1000-0", "same", origin="clean"),
        _e_text("2000-0", "same", origin="clean"),
        _e_text("3000-0", "long version", origin="messy"),
        _e_text("4000-0", "short", origin="messy"),
    ])
    assert plan["delete"] == ["1000-0"]
    assert list(plan["ambiguous"]) == [("u1", "messy", "meeting_summary")]


# --------------------------------------------------------------------
# The TYPE belongs in the key (measured on prod, 2026-09-14)
# --------------------------------------------------------------------

def _typed(user, origin, kind, text="x"):
    p = _payload(user, origin, text)
    p["type"] = kind
    return p


@pytest.mark.asyncio
async def test_an_analysis_does_not_block_a_transcript_for_the_same_meeting():
    """THE BUG THE FIRST KEY HAD. 102 origins on prod carry BOTH an
    `analysis` and a `meeting_transcript`: two different records of one
    meeting, not two deliveries of one record. Keyed on (user, origin)
    alone, a marked replay of the transcript was refused because the
    analysis had already landed, and the transcript never arrived."""
    r = FakeRedis()
    await _post(r, _typed("u1", "m-1", "analysis"))
    verdict = await _post(r, _typed("u1", "m-1", "meeting_transcript"),
                          marker="pending-ingest-sweep")
    assert verdict["write"] is True
    assert len(r.streams[STREAM_KEY]) == 2


@pytest.mark.asyncio
async def test_the_same_type_twice_is_still_deduplicated():
    r = FakeRedis()
    await _post(r, _typed("u1", "m-1", "meeting_transcript"))
    verdict = await _post(r, _typed("u1", "m-1", "meeting_transcript"),
                          marker="pending-ingest-sweep")
    assert verdict["write"] is False
    assert len(r.streams[STREAM_KEY]) == 1


@pytest.mark.asyncio
async def test_the_index_member_keeps_the_types_apart():
    r = FakeRedis()
    await _post(r, _typed("u1", "m-1", "analysis"))
    assert await r.sismember(origins_key("u1"), "m-1::analysis")
    assert not await r.sismember(origins_key("u1"), "m-1::meeting_transcript")


def test_cleanup_does_not_treat_two_types_as_duplicates():
    plan = plan_dedupe([
        ("1000-0", json.dumps(_typed("u1", "m-1", "analysis"))),
        ("2000-0", json.dumps(_typed("u1", "m-1", "meeting_transcript"))),
    ])
    assert plan["delete"] == []
    assert plan["ambiguous"] == {}
    assert len(plan["keep"]) == 2
