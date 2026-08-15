"""The dedup re-observation path records WHAT was restated, for
completables, and changes nothing for anything else.

Before this, re-observation stored three timestamps and a counter: the
fact that an item had been said again, with nothing about what was said.
That is the difference between an item nobody has mentioned since and an
item that comes back every month as a differently shaped fresh
commitment with its state unchanged, which is the whole feature.

The control test is the important one. The sink is the destructive end
of CQ, and the promise made with this change is that a patch which is
not a completable takes exactly the writes it took before.
"""

import sys
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("asyncpg", MagicMock())

from contextquilt.services import facet_runtime  # noqa: E402
from worker import store_connected_patches  # noqa: E402

MEETING = "22222222-2222-2222-2222-222222222222"
EXISTING = "33333333-3333-3333-3333-333333333333"


class FakeDB:
    """Enough asyncpg surface for the sink, with every statement recorded."""

    def __init__(self, dedup_row=None):
        self.dedup_row = dedup_row
        self.executed: list = []

    async def fetch(self, sql, *args):
        return []  # no registry rows: the SS floor decides completability

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


async def _store(db, patches, origin=MEETING):
    return await store_connected_patches(
        db, "user-1", patches, "meeting_summary", None, None,
        "Northwind", "proj-1", origin, "meeting",
    )


class TestRestatementRecord:
    async def test_a_restated_commitment_records_what_was_said(self):
        db = FakeDB(dedup_row=_hit())
        stored = await _store(db, [{
            "type": "commitment",
            "value": {
                "text": "Send the vendor shortlist over",
                "owner": "Marcus",
                "deadline": "end of next week",
                "deadline_date": "2026-08-21",
            },
        }])
        # Still a dedup: the point is that no second patch appears.
        assert stored == 0
        writes = db.matching("'{restatements}'")
        assert len(writes) == 1
        args = writes[0][1]
        assert args[0] == EXISTING
        # observed_at, then the item AS SPOKEN in this meeting.
        assert args[2] == "Send the vendor shortlist over"
        assert args[3] == "Marcus"
        assert args[4] == "end of next week"
        assert args[5] == "2026-08-21"
        # The receipt, and the same-meeting idempotency key.
        assert args[6] == MEETING

    async def test_the_existing_text_still_wins(self):
        # History, not an edit. Nothing in the re-observation path may
        # rewrite value.text with the newer phrasing.
        db = FakeDB(dedup_row=_hit())
        await _store(db, [{
            "type": "commitment",
            "value": {"text": "Send the vendor shortlist over", "owner": "Marcus"},
        }])
        assert not db.matching("'{text}'")

    async def test_the_count_is_bumped_alongside_the_capped_array(self):
        # The array is capped at ten; the counter is what keeps an item
        # restated fourteen times honest about fourteen.
        db = FakeDB(dedup_row=_hit())
        await _store(db, [{
            "type": "commitment",
            "value": {"text": "Send the vendor shortlist over", "owner": "Marcus"},
        }])
        sql = db.matching("'{restatements}'")[0][0]
        assert "'{restatement_count}'" in sql
        assert "jsonb_array_length" in sql and ">= 10" in sql

    async def test_the_same_meeting_cannot_count_twice(self):
        # A re-ingest of one transcript, or a second phrasing of one
        # sentence landing on the same patch, is not a second hop months
        # later. The guard is in the statement's WHERE clause.
        db = FakeDB(dedup_row=_hit())
        await _store(db, [{
            "type": "commitment",
            "value": {"text": "Send the vendor shortlist over"},
        }])
        sql = db.matching("'{restatements}'")[0][0]
        assert "COALESCE(origin_id, '') <> $7" in sql
        assert "value->'restatements'->-1->>'origin_id', '' ) <> $7" in sql

    async def test_a_blocker_records_too(self):
        # Completability comes from the runtime, not from a hardcoded
        # "commitment": blocker is the other SS completable.
        db = FakeDB(dedup_row=_hit("Legal review is blocking the shortlist"))
        await _store(db, [{
            "type": "blocker",
            "value": {"text": "Legal review is still blocking the shortlist"},
        }])
        assert len(db.matching("'{restatements}'")) == 1


class TestOwnerChange:
    async def test_a_new_owner_is_stamped_once(self):
        db = FakeDB(dedup_row=_hit())
        await _store(db, [{
            "type": "commitment",
            "value": {"text": "Send the vendor shortlist over", "owner": "Scott"},
        }])
        stamps = db.matching("'{owner_restated_at}'")
        assert len(stamps) == 1
        sql, args = stamps[0]
        assert args[2] == "Scott"
        # Once only, and never a rewrite of value.owner itself: the
        # ledger matches on that string, so editing it would move the
        # item off the ledger of the person the user is owed by.
        assert "value->>'owner_restated_at' IS NULL" in sql
        assert "lower(btrim(COALESCE(value->>'owner', ''))) <> lower(btrim($3::text))" in sql
        assert not db.matching("SET value = jsonb_set(value, '{owner}'")

    async def test_no_owner_on_the_restatement_stamps_nothing(self):
        # A blank owner means the extractor named nobody, which is not a
        # handover to nobody.
        db = FakeDB(dedup_row=_hit())
        await _store(db, [{
            "type": "commitment",
            "value": {"text": "Send the vendor shortlist over", "owner": "   "},
        }])
        assert db.matching("'{owner_restated_at}'") == []


class TestNonCompletablesAreUntouched:
    NON_COMPLETABLE = [
        ("takeaway", {"text": "The vendor list matters more than the timeline"}),
        ("preference", {"text": "Prefers written updates"}),
        ("decision", {"text": "Going with the incumbent vendor"}),
    ]

    @pytest.mark.parametrize("patch_type,value", NON_COMPLETABLE)
    async def test_no_restatement_history_is_written(self, patch_type, value):
        db = FakeDB(dedup_row=_hit(value["text"]))
        await _store(db, [{"type": patch_type, "value": dict(value, owner="Marcus")}])
        assert db.matching("'{restatements}'") == []
        assert db.matching("'{owner_restated_at}'") == []

    async def test_the_re_observation_writes_are_exactly_what_they_were(self):
        # The control, stated as the whole statement list rather than as
        # an absence: a re-observed takeaway takes the freshness bump and
        # the usage bump, in that order, and nothing else.
        db = FakeDB(dedup_row=_hit("The vendor list matters more than the timeline"))
        await _store(db, [{
            "type": "takeaway",
            "value": {"text": "The vendor list matters more than the timeline"},
        }])
        statements = [" ".join(sql.split()) for sql, _ in db.executed]
        assert statements == [
            "INSERT INTO profiles (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
            "UPDATE context_patches SET updated_at = $1, last_observed_at = $1 "
            "WHERE patch_id = $2::uuid "
            "AND ($3::text IS NULL OR COALESCE(origin_id, '') <> $3::text)",
            "UPDATE patch_usage_metrics SET access_count = access_count + 1, "
            "last_accessed_at = $1 WHERE patch_id = $2::uuid "
            "AND ($3::text IS NULL OR NOT EXISTS ( "
            "SELECT 1 FROM context_patches cp WHERE cp.patch_id = $2::uuid "
            "AND COALESCE(cp.origin_id, '') = $3::text ))",
        ]

    async def test_a_completable_keeps_those_same_writes_first(self):
        # The addition is additive: the freshness and usage bumps still
        # happen, and still happen first.
        db = FakeDB(dedup_row=_hit())
        await _store(db, [{
            "type": "commitment",
            "value": {"text": "Send the vendor shortlist over", "owner": "Marcus"},
        }])
        assert db.executed[1][0].startswith(
            "UPDATE context_patches SET updated_at = $1, last_observed_at = $1"
        )
        assert db.executed[2][0].startswith("UPDATE patch_usage_metrics")


class TestNewPatchesAreNotRestatements:
    async def test_a_first_statement_records_nothing(self):
        # No dedup hit: this item has never been said before, so there is
        # no history to write and an insert is all that happens.
        db = FakeDB(dedup_row=None)
        stored = await _store(db, [{
            "type": "commitment",
            "value": {"text": "Send the vendor shortlist over", "owner": "Marcus"},
        }])
        assert stored == 1
        assert db.matching("'{restatements}'") == []
