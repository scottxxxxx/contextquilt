"""The storage sink honors the two manifest flags, and nothing else moves.

`store_connected_patches` is the destructive end of this feature. Its
trigram fast path is type blind: it merges two same-type patches on text
similarity alone, which is right for a fact restated in a second meeting
and wrong for an observation of behavior, where the second sighting IS
the evidence. Worse, a collapse keeps only the surviving patch's
origin_id, so it silently destroys a receipt, and the profile pass gates
on distinct meetings.

The sink also stamps origin_id only on project-scoped types, so a type
that is meeting-bound without being project-bound lands with a null
origin and is structurally invisible to the person cluster query, which
requires `origin_id IS NOT NULL`.

These tests drive the real function against a fake connection and assert
both, plus the control: with the flags absent, every query and every
insert argument is what it was before.
"""

import sys
from unittest.mock import MagicMock

import pytest

# The sink lives in worker.py, which imports asyncpg at module scope.
# setdefault, so a real asyncpg (docker, CI) is used untouched and only a
# machine without the driver gets the stand-in.
sys.modules.setdefault("asyncpg", MagicMock())

from contextquilt.services import facet_runtime  # noqa: E402
from worker import store_connected_patches  # noqa: E402

MEETING = "11111111-1111-1111-1111-111111111111"
REGISTRY_ROWS = [
    {
        "type_key": "moment", "facet": "Episode",
        "is_completable": False, "project_scoped": False,
        "default_ttl_days": 90,
    },
]


class FakeDB:
    """Enough asyncpg surface for the sink, with the calls recorded.

    `dedup_row` is what the candidate query returns when it is asked. The
    point of most of these tests is whether it gets asked at all.
    """

    def __init__(self, dedup_row=None):
        self.dedup_row = dedup_row
        self.executed: list = []
        self.dedup_queries: int = 0

    async def fetch(self, sql, *args):
        if "patch_type_registry" in sql:
            return REGISTRY_ROWS
        return []

    async def fetchrow(self, sql, *args):
        if "SIMILARITY" in sql:
            self.dedup_queries += 1
            return self.dedup_row
        return None

    async def execute(self, sql, *args):
        self.executed.append((" ".join(sql.split()), args))
        return "INSERT 0 1"

    # -- assertions helpers ------------------------------------------
    def inserts(self):
        return [
            args for sql, args in self.executed
            if sql.startswith("INSERT INTO context_patches")
        ]

    def reobservations(self):
        return [
            args for sql, args in self.executed
            if sql.startswith("UPDATE context_patches SET updated_at")
        ]


@pytest.fixture(autouse=True)
def _clean_runtime_cache():
    # The runtime snapshot is process-global and five minutes long; a
    # stale one would silently decide project scoping for these tests.
    facet_runtime.invalidate_type_runtime()
    yield
    facet_runtime.invalidate_type_runtime()


def _observation(text):
    return {"type": "moment", "value": {"text": text, "owner": "Denby"}}


async def _store(db, patches, **kwargs):
    return await store_connected_patches(
        db, "user-1", patches, "meeting_summary", None, None,
        "Northwind", "proj-1", MEETING, "meeting",
        **kwargs,
    )


class TestNoCollapse:
    async def test_a_flagged_type_never_reaches_the_dedup_query(self):
        # A candidate is waiting with a similarity of 0.95. If the query
        # ran, the observation would be merged into it and one meeting
        # would vanish from the person's receipts.
        db = FakeDB(dedup_row={
            "patch_id": "existing", "existing_text": "Asked for the numbers first",
            "project_id": "proj-1", "sim": 0.95,
        })
        stored = await _store(
            db,
            [_observation("Asked for the cost numbers before agreeing")],
            no_collapse_types={"moment"},
            origin_scoped_types={"moment"},
        )
        assert stored == 1
        assert db.dedup_queries == 0
        assert db.reobservations() == []
        assert len(db.inserts()) == 1

    async def test_two_sightings_of_one_behavior_are_two_patches(self):
        db = FakeDB(dedup_row={
            "patch_id": "existing", "existing_text": "Asked for the numbers first",
            "project_id": "proj-1", "sim": 0.99,
        })
        stored = await _store(
            db,
            [
                _observation("Asked for the cost numbers before agreeing"),
                _observation("Asked for the cost numbers before agreeing again"),
            ],
            no_collapse_types={"moment"},
            origin_scoped_types={"moment"},
        )
        assert stored == 2

    async def test_without_the_flag_the_same_patch_collapses(self):
        # The control. Same input, no flag: today's behavior, unchanged.
        db = FakeDB(dedup_row={
            "patch_id": "existing", "existing_text": "Asked for the numbers first",
            "project_id": "proj-1", "sim": 0.95,
        })
        stored = await _store(db, [_observation("Asked for the cost numbers before agreeing")])
        assert stored == 0
        assert db.dedup_queries == 1
        assert len(db.reobservations()) == 1
        assert db.inserts() == []

    async def test_unflagged_types_still_dedup_in_the_same_batch(self):
        db = FakeDB(dedup_row={
            "patch_id": "existing", "existing_text": "Ship the API by Friday",
            "project_id": "proj-1", "sim": 0.9,
        })
        stored = await _store(
            db,
            [
                _observation("Asked for the cost numbers before agreeing"),
                {"type": "commitment", "value": {"text": "Ship the API by Friday"}},
            ],
            no_collapse_types={"moment"},
            origin_scoped_types={"moment"},
        )
        # The observation inserted, the commitment collapsed.
        assert stored == 1
        assert db.dedup_queries == 1


class TestOriginStamp:
    # Insert argument order, from _insert_new_patch:
    #   0 patch_id, 1 patch_name, 2 patch_type, 3 value,
    #   4 origin_mode, 5 source_prompt, 6 confidence, 7 persistence,
    #   8 project, 9 project_id, 10 origin_id, 11 origin_type
    async def test_flagged_type_carries_the_meeting_without_the_project(self):
        db = FakeDB()
        await _store(
            db, [_observation("Asked for the cost numbers before agreeing")],
            no_collapse_types={"moment"}, origin_scoped_types={"moment"},
        )
        args = db.inserts()[0]
        assert args[10] == MEETING
        assert args[11] == "meeting"
        # Not project scoped, and the flag must not have changed that.
        assert args[8] is None
        assert args[9] is None

    async def test_without_the_flag_the_origin_is_null(self):
        # The bug this flag exists for: an observation with no origin_id
        # can never be counted as a receipt, so the person cluster query
        # skips it entirely.
        db = FakeDB()
        await _store(db, [_observation("Asked for the cost numbers before agreeing")])
        args = db.inserts()[0]
        assert args[10] is None
        assert args[11] is None

    async def test_project_scoped_types_are_unaffected(self):
        # A commitment carried project and origin before the flag existed
        # and carries exactly the same after it.
        db = FakeDB()
        await _store(
            db, [{"type": "commitment", "value": {"text": "Ship the API by Friday"}}],
            no_collapse_types={"moment"}, origin_scoped_types={"moment"},
        )
        args = db.inserts()[0]
        assert args[8] == "Northwind"
        assert args[9] == "proj-1"
        assert args[10] == MEETING
