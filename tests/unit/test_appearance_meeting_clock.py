"""A presence row is dated by the MEETING, never by the ingest.

Doc 16 6.2a states this and only the relabel routes implemented it. The
ingest path took the column default on insert and stamped NOW() on
conflict, which is right exactly once, on a meeting's first ingest.

Proven wrong on 2026-08-15: replaying real meetings to repair the entity
regression wrote presence rows dated the replay, so people last met in
July rendered as met today across 23 meetings. The repair told the truth
about who was in the room and lied about when, which on a screen whose
most prominent field is "last met" is the worse half.
"""

import sys
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("asyncpg", MagicMock())

from contextquilt.services import facet_runtime  # noqa: E402
import worker  # noqa: E402

MEETING = "22222222-2222-2222-2222-222222222222"


class FakeDB:
    def __init__(self):
        self.executed: list = []

    async def fetch(self, sql, *args):
        return []

    async def fetchrow(self, sql, *args):
        return None

    async def fetchval(self, sql, *args):
        # entities.suppressed_at lookup: not suppressed
        return None

    async def execute(self, sql, *args):
        self.executed.append((" ".join(sql.split()), args))
        return "INSERT 0 1"

    def matching(self, needle):
        return [(sql, args) for sql, args in self.executed if needle in sql]


@pytest.fixture(autouse=True)
def _clean_runtime_cache():
    facet_runtime.invalidate_type_runtime()
    yield
    facet_runtime.invalidate_type_runtime()


def _appearance_sql():
    """The INSERT as it will run, read off the module source rather than
    executed, because the statement is what carries the rule."""
    src = open(worker.__file__).read()
    start = src.index("INSERT INTO person_appearances")
    end = src.index("RETURNING", start) if "RETURNING" in src[start:start + 6000] else start + 6000
    return " ".join(src[start:end].split())


class TestTheClockComesFromTheMeeting:
    def test_the_insert_states_both_dates_rather_than_defaulting(self):
        sql = _appearance_sql()
        assert "first_seen_at, last_seen_at" in sql

    def test_sibling_rows_win_first(self):
        # A meeting that already has presence rows already knows its clock.
        sql = _appearance_sql()
        assert "SELECT min(pa2.first_seen_at) FROM person_appearances pa2" in sql
        assert "pa2.origin_id = $3" in sql

    def test_the_meetings_own_patches_are_the_fallback(self):
        # The patches were written by the original ingest and carry its
        # clock, so they date the meeting even when no presence row does.
        # A replay only ADDS patches, so the oldest one is still the
        # original ingest.
        sql = _appearance_sql()
        assert "SELECT min(cp.created_at) FROM context_patches cp" in sql
        assert "cp.origin_id = $3" in sql

    def test_now_is_the_last_resort_not_the_default(self):
        sql = _appearance_sql()
        clock = sql[sql.index("COALESCE("):]
        assert clock.index("person_appearances pa2") < clock.index("NOW()")
        assert clock.index("context_patches cp") < clock.index("NOW()")


class TestReingestDoesNotMoveTheDate:
    def test_the_conflict_branch_never_stamps_last_seen_at(self):
        # One row is one person in one meeting. A second ingest of that
        # meeting is the same observation arriving twice, so there is no
        # later observation to record. Same principle as the same-origin
        # guard on patch freshness, through the door that one missed.
        sql = _appearance_sql()
        conflict = sql[sql.index("ON CONFLICT"):]
        assert "last_seen_at = NOW()" not in conflict
        assert "last_seen_at =" not in conflict

    def test_the_other_conflict_merges_are_untouched(self):
        # The guard must not cost the capacity union or the max-not-sum
        # rules: a re-ingest that observes someone speaking still records
        # that they spoke.
        sql = _appearance_sql()
        conflict = sql[sql.index("ON CONFLICT"):]
        assert "capacities = ARRAY(SELECT DISTINCT unnest(" in conflict
        assert "GREATEST(COALESCE(person_appearances.turn_count, 0)" in conflict
