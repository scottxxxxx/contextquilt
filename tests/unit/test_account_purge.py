"""Unit tests for account_purge pure logic (cq-tier-signals lane)."""

import json

from src.contextquilt.services.account_purge import (
    ACTION_INCONSISTENT,
    ACTION_PURGED,
    ACTION_RECORD_ONLY,
    classify_signal,
    stream_entry_is_users,
)


# ------------------------------------------------------------------
# classify_signal — never destructive on a malformed request
# ------------------------------------------------------------------

def test_consistent_account_deleted_purges():
    assert classify_signal("account_deleted", "deleted") == ACTION_PURGED


def test_case_and_whitespace_tolerant():
    assert classify_signal(" Account_Deleted ", " DELETED ") == ACTION_PURGED


def test_deletion_event_with_wrong_tier_is_inconsistent():
    # Claims deletion but the tier disagrees — a purge on this shape
    # would be destructive on a malformed request.
    assert classify_signal("account_deleted", "pro") == ACTION_INCONSISTENT
    assert classify_signal("account_deleted", None) == ACTION_INCONSISTENT
    assert classify_signal("account_deleted", "") == ACTION_INCONSISTENT


def test_deleted_tier_without_deletion_event_is_inconsistent():
    assert classify_signal("downgrade", "deleted") == ACTION_INCONSISTENT


def test_ordinary_vocabulary_is_record_only():
    for et in ("upgrade", "downgrade", "trial_start", "trial_to_paid",
               "cancellation", "expire", "refund"):
        assert classify_signal(et, "pro") == ACTION_RECORD_ONLY


def test_unknown_event_types_are_record_only():
    assert classify_signal("mystery_future_event", "plus") == ACTION_RECORD_ONLY
    assert classify_signal("", None) == ACTION_RECORD_ONLY


def test_non_string_tier_never_purges():
    assert classify_signal("account_deleted", 42) == ACTION_INCONSISTENT
    assert classify_signal("account_deleted", {"tier": "deleted"}) == ACTION_INCONSISTENT


# ------------------------------------------------------------------
# stream_entry_is_users — parse guesses never match
# ------------------------------------------------------------------

def test_capture_payload_matches_its_user():
    data = json.dumps({"user_id": "u-1", "content": "transcript...", "app_id": "a"})
    assert stream_entry_is_users(data, "u-1") is True
    assert stream_entry_is_users(data, "u-2") is False


def test_hydrate_marker_matches_its_user():
    data = json.dumps({"type": "hydrate", "user_id": "u-1", "timestamp": "t"})
    assert stream_entry_is_users(data, "u-1") is True


def test_malformed_entries_never_match():
    assert stream_entry_is_users("not json {", "u-1") is False
    assert stream_entry_is_users(None, "u-1") is False
    assert stream_entry_is_users("", "u-1") is False
    assert stream_entry_is_users(json.dumps(["user_id", "u-1"]), "u-1") is False


def test_missing_user_id_never_matches():
    assert stream_entry_is_users(json.dumps({"content": "x"}), "u-1") is False
