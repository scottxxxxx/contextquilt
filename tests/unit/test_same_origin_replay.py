"""A re-ingest of the SAME meeting must not move a freshness anchor.

The 2026-06-10 entity regression has to be repaired by replaying real
meetings CQ already holds. Without this guard, one afternoon of replay
re-anchors decay across two months of history in a single write pass,
and every stale item on the user's screen reads as fresh. That is the
one failure a second pass cannot undo, and unlike an entity count it is
a thing the user would see and correctly disbelieve.

The guard is in the SQL rather than in a read-then-branch, so these
tests assert on the statements the sink emits, the same way the
restatement control test does.
"""

import sys
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("asyncpg", MagicMock())

from contextquilt.services import facet_runtime  # noqa: E402
from worker import store_connected_patches  # noqa: E402

MEETING = "22222222-2222-2222-2222-222222222222"
OTHER_MEETING = "44444444-4444-4444-4444-444444444444"
EXISTING = "33333333-3333-3333-3333-333333333333"


class FakeDB:
    def __init__(self, dedup_row=None):
        self.dedup_row = dedup_row
        self.executed: list = []

    async def fetch(self, sql, *args):
        return []

    async def fetchrow(self, sql, *args):
        if "SIMILARITY" in sql:
            return self.dedup_row
        return None

    async def execute(self, sql, *args):
        self.executed.append((" ".join(sql.split()), args))
        return "UPDATE 1"

    def matching(self, needle):
        return [(sql, args) for sql, args in self.executed if needle in sql]


@pytest.fixture(autouse=True)
def _clean_runtime_cache():
    facet_runtime.invalidate_type_runtime()
    yield
    facet_runtime.invalidate_type_runtime()


def _hit(text="Send the vendor shortlist"):
    return {
        "patch_id": EXISTING, "existing_text": text,
        "project_id": "proj-1", "sim": 0.95,
    }


async def _store(db, patches, origin):
    return await store_connected_patches(
        db, "user-1", patches, "meeting_summary", None, None,
        "Northwind", "proj-1", origin, "meeting",
    )


def _commitment(text="Send the vendor shortlist over"):
    return [{"type": "commitment", "value": {"text": text, "owner": "Marcus"}}]


class TestTheGuardIsCarried:
    async def test_the_freshness_write_is_guarded_by_the_incoming_origin(self):
        db = FakeDB(dedup_row=_hit())
        await _store(db, _commitment(), MEETING)
        writes = db.matching("last_observed_at = $1")
        assert len(writes) == 1
        sql, args = writes[0]
        assert "COALESCE(origin_id, '') <> $3::text" in sql
        assert args[2] == MEETING

    async def test_the_usage_write_is_guarded_the_same_way(self):
        # patch_usage_metrics.last_accessed_at exempts a patch from decay,
        # so leaving it unguarded would re-anchor the replay set through
        # the other door.
        db = FakeDB(dedup_row=_hit())
        await _store(db, _commitment(), MEETING)
        writes = db.matching("patch_usage_metrics")
        assert len(writes) == 1
        sql, args = writes[0]
        assert "NOT EXISTS" in sql
        assert args[2] == MEETING

    async def test_a_different_meeting_carries_its_own_origin(self):
        # The guard compares the STORED origin against the incoming one,
        # so a genuine second observation from a different meeting passes
        # it and the anchors move as they always did.
        db = FakeDB(dedup_row=_hit())
        await _store(db, _commitment(), OTHER_MEETING)
        sql, args = db.matching("last_observed_at = $1")[0]
        assert args[2] == OTHER_MEETING


class TestOriginlessIngest:
    async def test_a_null_origin_never_suppresses_the_bump(self):
        # User-scoped types land origin-null by design. A null incoming
        # origin must read as "cannot be the same meeting", never as a
        # match, or chat-lane re-observation would stop anchoring.
        db = FakeDB(dedup_row=_hit())
        await _store(db, _commitment(), None)
        sql, args = db.matching("last_observed_at = $1")[0]
        assert args[2] is None
        assert "$3::text IS NULL OR" in sql


class TestDetailStillMergesForward:
    async def test_a_replay_that_learns_a_date_still_records_it(self):
        # The guard holds back timestamps, not knowledge. Deadline detail,
        # salience upgrades and restatements write their own rows and are
        # untouched, so a replay that genuinely learns something keeps it.
        db = FakeDB(dedup_row=_hit())
        await _store(db, [{
            "type": "commitment",
            "value": {
                "text": "Send the vendor shortlist over",
                "owner": "Marcus",
                "deadline": "Friday",
                "deadline_date": "2026-08-21",
            },
        }], MEETING)
        assert db.matching("'{deadline_date}'")

    async def test_the_same_meeting_still_cannot_log_a_restatement(self):
        # Pre-existing guard, re-asserted here because the replay plan
        # leans on both of them meaning the same thing by "same meeting".
        db = FakeDB(dedup_row=_hit())
        await _store(db, _commitment(), MEETING)
        sql, args = db.matching("'{restatements}'")[0]
        assert "COALESCE(origin_id, '') <> $7" in sql
        assert args[6] == MEETING
