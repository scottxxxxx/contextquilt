"""Unit tests for the alerting service.

Covers the pure-function pieces (fingerprint computation, category
catalog shape, email HTML rendering, IncidentReport dataclass). The
DB-dependent behavior (dedup, auto-resolve, ON CONFLICT race
handling, recipient subscription filtering) needs a live Postgres and
is exercised via integration tests in tests/e2e/ — kept separate so
the unit suite stays fast and runnable without Docker.

The wire-in pattern (worker._maybe_alert_llm_failure) is validated by
deploy-time smoke test: rotate to an intentionally bad key, push a
small transcript through /v1/memory, confirm the alert email lands.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from src.contextquilt.services.alerting import (
    INCIDENT_AUTO_RESOLVE_MINUTES,
    KNOWN_CATEGORIES,
    IncidentReport,
    _fingerprint,
    _render_email_html,
    sweep_stale_incidents,
)


class TestFingerprint:
    def test_combines_category_and_subject(self):
        assert _fingerprint("provider_auth_failed", "openrouter") == "provider_auth_failed:openrouter"

    def test_handles_subject_with_colons(self):
        # Subjects can contain colons (e.g. URLs); fingerprint just
        # concatenates. Uniqueness is still preserved because category
        # is a stable token from KNOWN_CATEGORIES.
        assert _fingerprint("backup_failed", "daily:2026-05-20") == "backup_failed:daily:2026-05-20"

    def test_empty_subject_is_still_unique_per_category(self):
        assert _fingerprint("provider_auth_failed", "") != _fingerprint("provider_budget_exhausted", "")


class TestKnownCategories:
    def test_includes_the_four_expected_categories(self):
        # Three from GP for cross-stack parity, one CQ-specific.
        assert "cq_unreachable" in KNOWN_CATEGORIES
        assert "provider_auth_failed" in KNOWN_CATEGORIES
        assert "provider_budget_exhausted" in KNOWN_CATEGORIES
        assert "backup_failed" in KNOWN_CATEGORIES

    def test_each_category_has_label_and_description(self):
        for cat, meta in KNOWN_CATEGORIES.items():
            assert "label" in meta, f"{cat} missing label"
            assert "description" in meta, f"{cat} missing description"
            assert meta["label"], f"{cat} has empty label"
            assert meta["description"], f"{cat} has empty description"

    def test_auto_resolve_window_matches_gp_for_cross_stack_parity(self):
        # GP set 30 minutes; we match so re-fire behavior is consistent
        # if we ever combine dashboards. Diverging would be a footgun.
        assert INCIDENT_AUTO_RESOLVE_MINUTES == 30


class TestRenderEmailHtml:
    def test_subject_line_uses_contextquilt_brand(self):
        # GP uses [GhostPour]; we must use [ContextQuilt] so recipients
        # can identify which stack alerted them at a glance.
        subject_line, _ = _render_email_html(
            category="provider_auth_failed",
            subject="openrouter",
            details={},
            incident_id="abc-123",
            first_seen_at=datetime.now(timezone.utc),
        )
        assert subject_line.startswith("[ContextQuilt]")
        assert "openrouter" in subject_line

    def test_uses_category_label_not_raw_id_in_subject(self):
        subject_line, _ = _render_email_html(
            category="provider_auth_failed",
            subject="openrouter",
            details={},
            incident_id="abc",
            first_seen_at=datetime.now(timezone.utc),
        )
        assert "Managed LLM key rejected" in subject_line

    def test_unknown_category_falls_back_to_raw_id(self):
        # Defensive: an unrecognized category shouldn't crash rendering.
        subject_line, html = _render_email_html(
            category="novel_failure",
            subject="x",
            details={},
            incident_id="abc",
            first_seen_at=datetime.now(timezone.utc),
        )
        assert "novel_failure" in subject_line
        assert html  # rendered without raising

    def test_html_body_includes_incident_id(self):
        _, html = _render_email_html(
            category="provider_auth_failed",
            subject="openrouter",
            details={},
            incident_id="incident-xyz-789",
            first_seen_at=datetime.now(timezone.utc),
        )
        assert "incident-xyz-789" in html

    def test_details_dict_rendered_as_table_rows(self):
        _, html = _render_email_html(
            category="provider_auth_failed",
            subject="openrouter",
            details={"status_code": 403, "model": "anthropic/claude-haiku-4.5"},
            incident_id="abc",
            first_seen_at=datetime.now(timezone.utc),
        )
        assert "status_code" in html
        assert "403" in html
        assert "model" in html
        assert "anthropic/claude-haiku-4.5" in html

    def test_long_detail_value_truncated_to_keep_email_small(self):
        # 500 chars + ellipsis cap so a giant response body in details
        # doesn't bloat the email past mail-server size limits.
        big = "z" * 1000
        _, html = _render_email_html(
            category="provider_auth_failed",
            subject="openrouter",
            details={"response_body": big},
            incident_id="abc",
            first_seen_at=datetime.now(timezone.utc),
        )
        # The full 1000-char run must not appear (truncation fired).
        assert "z" * 1000 not in html
        # Exactly 500 z's followed by the ellipsis must appear.
        assert "z" * 500 + "…" in html

    def test_handles_empty_details(self):
        # Most production callers pass details={...} but None is valid.
        subject_line, html = _render_email_html(
            category="provider_auth_failed",
            subject="openrouter",
            details=None,
            incident_id="abc",
            first_seen_at=datetime.now(timezone.utc),
        )
        assert subject_line
        assert html


class TestIncidentReportDataclass:
    def test_default_suppressed_reason_is_none(self):
        r = IncidentReport(incident_id="x", is_new=True, emailed_to=["a@b.c"])
        assert r.suppressed_reason is None

    def test_carries_suppressed_reason_when_set(self):
        r = IncidentReport(
            incident_id="x", is_new=False, emailed_to=[],
            suppressed_reason="incident_already_open",
        )
        assert r.suppressed_reason == "incident_already_open"


class _FakeConn:
    """Minimal asyncpg-connection stand-in: records the execute call and
    returns a canned status string, the way asyncpg's .execute does
    ("UPDATE N"). Lets us unit-test the resolver query shape and the
    count-parsing without a live Postgres."""

    def __init__(self, status: str = "UPDATE 0"):
        self._status = status
        self.calls: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args):
        self.calls.append((sql, args))
        return self._status


class TestSweepStaleIncidents:
    """The periodic resolver. Its query shape and count-parsing are pure
    enough to test with a fake conn; the actual row resolution is covered
    by the e2e suite against live Postgres."""

    def test_returns_parsed_resolved_count(self):
        conn = _FakeConn("UPDATE 3")
        assert asyncio.run(sweep_stale_incidents(conn)) == 3

    def test_updates_open_incidents_filtered_by_quiet_window(self):
        conn = _FakeConn("UPDATE 1")
        asyncio.run(sweep_stale_incidents(conn))
        sql, args = conn.calls[0]
        assert "UPDATE alert_incidents" in sql
        assert "resolved_at IS NULL" in sql
        assert "last_seen_at <" in sql
        # (resolved_at value, cutoff)
        assert len(args) == 2

    def test_cutoff_is_now_minus_auto_resolve_window(self):
        conn = _FakeConn("UPDATE 0")
        asyncio.run(sweep_stale_incidents(conn))
        resolved_at_arg, cutoff_arg = conn.calls[0][1]
        delta_min = (resolved_at_arg - cutoff_arg).total_seconds() / 60
        assert round(delta_min) == INCIDENT_AUTO_RESOLVE_MINUTES

    def test_unparseable_status_string_returns_zero(self):
        # Defensive: a non-"UPDATE N" status must not raise into the loop.
        conn = _FakeConn("WEIRD")
        assert asyncio.run(sweep_stale_incidents(conn)) == 0
