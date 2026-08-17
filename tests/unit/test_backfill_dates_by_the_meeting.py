"""The backfill dates a presence row by the meeting, like the ingest does.

Doc 19.4, THIRD DOOR, and this one was opened by the fix for the second.

#247 fixed the INGEST path so an appearance row is dated by the meeting
rather than by the ingest clock. The backfill was not fixed and nobody
looked, because it was not the path that had failed. It supplied each
PATCH's own `created_at`, and the upsert takes GREATEST(existing,
incoming), so a row lands on the LATEST patch anchored to the meeting.

Within one ingest those differ by seconds. On a REPLAYED meeting they
differ by weeks: the meeting then holds patches from the original ingest
AND from the replay, and GREATEST picks the replay.

Measured 2026-08-17: running the ownership tier re-dated 160 rows to the
replay, hours after #247 shipped. Zero wrong before, 160 after, 0 again
after repair.
"""

import pathlib
import re

BACKFILL = pathlib.Path("scripts/backfill_person_appearances.py").read_text()


def test_both_ownership_queries_use_the_meeting_clock():
    """Not the patch's own created_at. One fixed query and one missed is
    the same bug with a smaller blast radius."""
    assert BACKFILL.count("OVER (PARTITION BY") == 2
    assert "min(cp.created_at) OVER (PARTITION BY cp.origin_id)" in BACKFILL
    assert "min(tgt.created_at) OVER (PARTITION BY tgt.origin_id)" in BACKFILL


def test_no_bare_patch_timestamp_is_selected_as_the_row_date():
    """The failure was selecting a per-row timestamp where a per-MEETING
    one was needed, so the guard is that no bare `X.created_at` is
    aliased into the writer's date argument."""
    for bad in ("cp.created_at, cp.value->>'owner'",
                "tgt.created_at, src.value->>'text'"):
        assert bad not in BACKFILL, f"bare timestamp still selected: {bad}"


def test_the_upsert_still_takes_the_earliest_and_latest_it_is_given():
    """LEAST/GREATEST are correct given a correct input, and were never
    the bug. Pinned so a later reader does not 'fix' the wrong half."""
    assert "first_seen_at = LEAST(" in BACKFILL
    assert "last_seen_at  = GREATEST(" in BACKFILL


def test_the_reason_is_recorded_where_the_next_person_will_look():
    """A rule stated in one place and implemented in another is a rule
    with one carrier (19.2), which is how the ingest path failed in the
    first place."""
    assert "#247" in BACKFILL and "19.4" in BACKFILL
