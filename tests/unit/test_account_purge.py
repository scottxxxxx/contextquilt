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


# =====================================================================
# Failure alerting (added 2026-08-02).
#
# The deletion lane had the exact blind spot GP had just disclosed in
# their own telemetry: a purge that failed every cycle logged and
# nothing else, so "no alerts" was indistinguishable from "no deletion
# requests". These cover the threshold and the two alert categories.
# =====================================================================

from contextquilt.services.account_purge import (  # noqa: E402
    PURGE_ALERT_AFTER_FAILURES,
    should_alert_for_failures,
)


def test_single_failure_does_not_alert():
    # One failure is usually a DB or Redis blip, and the consumer retries
    # the same signal in 60s regardless. Alerting here would train people
    # to ignore the category.
    assert should_alert_for_failures(1) is False


def test_alerts_once_failures_are_sustained():
    assert should_alert_for_failures(PURGE_ALERT_AFTER_FAILURES) is True
    assert should_alert_for_failures(PURGE_ALERT_AFTER_FAILURES + 5) is True


def test_threshold_is_short_enough_to_matter():
    # A stuck deletion means a user asked to be deleted and CQ still
    # holds their data. Minutes, not hours.
    assert 2 <= PURGE_ALERT_AFTER_FAILURES <= 5


def test_both_purge_alert_categories_are_registered():
    # report_incident logs a warning and records anyway for an unknown
    # category, so an unregistered one would still "work" while losing
    # its label and description in the email. Assert they are real.
    from contextquilt.services.alerting import KNOWN_CATEGORIES
    for cat in ("account_purge_failed", "account_purge_inconsistent"):
        assert cat in KNOWN_CATEGORIES, f"{cat} must be a known category"
        assert KNOWN_CATEGORIES[cat]["label"]
        assert KNOWN_CATEGORIES[cat]["description"]


def test_consumer_isolates_each_signal():
    """One signal that always throws must not starve the queue behind it.

    The batch is ordered by received_at, so before per-row isolation a
    permanently-failing signal meant nothing after it was ever processed:
    the exception escaped to the outer handler every cycle, forever.
    """
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "worker.py"
    body = src.read_text(encoding="utf-8").split("async def tier_signals_loop")[1]
    body = body.split("\n    async def ", 1)[0]

    assert "except Exception as row_exc:" in body, "per-signal handler missing"
    # The per-row handler has to sit inside the for loop, before the
    # outer one, or it is not isolating anything.
    assert body.index("for row in rows:") < body.index("except Exception as row_exc:")
    assert body.index("except Exception as row_exc:") < body.index("except Exception as e:")


def test_inconsistent_signals_alert_immediately():
    """Inconsistent signals are stamped and never retried, so waiting for
    a repeat would wait forever."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "worker.py"
    body = src.read_text(encoding="utf-8").split("async def tier_signals_loop")[1]
    body = body.split("\n    async def ", 1)[0]
    inconsistent = body.index("ACTION_INCONSISTENT")
    alert = body.index('"account_purge_inconsistent"')
    # Alert fires in the inconsistent branch, not gated behind the
    # consecutive-failure threshold.
    assert inconsistent < alert
    assert "should_alert_for_failures" not in body[inconsistent:alert]
